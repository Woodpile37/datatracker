# Copyright The IETF Trust 2012-2020, All Rights Reserved
# -*- coding: utf-8 -*-


import base64
import datetime
import re
import requests

from urllib.parse import urlencode
from xml.dom import pulldom, Node

from django.conf import settings
from django.utils import timezone
from django.utils.encoding import smart_bytes, force_str, force_text

import debug                            # pyflakes:ignore

from ietf.doc.models import ( Document, DocAlias, State, StateType, DocEvent, DocRelationshipName,
    DocTagName, DocTypeName, RelatedDocument )
from ietf.doc.expire import move_draft_files_to_archive
from ietf.doc.utils import add_state_change_event, prettify_std_name, update_action_holders
from ietf.group.models import Group
from ietf.name.models import StdLevelName, StreamName
from ietf.person.models import Person
from ietf.utils.log import log
from ietf.utils.mail import send_mail_text
from ietf.utils.timezone import datetime_from_date, RPC_TZINFO

#QUEUE_URL = "https://www.rfc-editor.org/queue2.xml"
#INDEX_URL = "https://www.rfc-editor.org/rfc/rfc-index.xml"
#POST_APPROVED_DRAFT_URL = "https://www.rfc-editor.org/sdev/jsonexp/jsonparser.php"

MIN_ERRATA_RESULTS = 5000
MIN_INDEX_RESULTS = 8000
MIN_QUEUE_RESULTS = 10

def get_child_text(parent_node, tag_name):
    text = []
    for node in parent_node.childNodes:
        if node.nodeType == Node.ELEMENT_NODE and node.localName == tag_name:
            text.append(node.firstChild.data)
    return '\n\n'.join(text)


def parse_queue(response):
    """Parse RFC Editor queue XML into a bunch of tuples + warnings."""

    events = pulldom.parse(response)
    drafts = []
    warnings = []
    stream = None

    for event, node in events:
        try:
            if event == pulldom.START_ELEMENT and node.tagName == "entry":
                events.expandNode(node)
                node.normalize()
                draft_name = get_child_text(node, "draft").strip()
                draft_name = re.sub(r"(-\d\d)?(.txt){1,2}$", "", draft_name)
                date_received = get_child_text(node, "date-received")

                state = ""
                tags = []
                missref_generation = ""
                for child in node.childNodes:
                    if child.nodeType == Node.ELEMENT_NODE and child.localName == "state":
                        state = child.firstChild.data
                        # state has some extra annotations encoded, parse
                        # them out
                        if '*R' in state:
                            tags.append("ref")
                            state = state.replace("*R", "")
                        if '*A' in state:
                            tags.append("iana")
                            state = state.replace("*A", "")
                        m = re.search(r"\(([0-9]+)G\)", state)
                        if m:
                            missref_generation = m.group(1)
                            state = state.replace("(%sG)" % missref_generation, "")

                # AUTH48 link
                auth48 = ""
                for child in node.childNodes:
                    if child.nodeType == Node.ELEMENT_NODE and child.localName == "auth48-url":
                        auth48 = child.firstChild.data

                # cluster link (if it ever gets implemented)
                cluster = ""
                for child in node.childNodes:
                    if child.nodeType == Node.ELEMENT_NODE and child.localName == "cluster-url":
                        cluster = child.firstChild.data

                refs = []
                for child in node.childNodes:
                    if child.nodeType == Node.ELEMENT_NODE and child.localName == "normRef":
                        ref_name = get_child_text(child, "ref-name")
                        ref_state = get_child_text(child, "ref-state")
                        in_queue = ref_state.startswith("IN-QUEUE")
                        refs.append((ref_name, ref_state, in_queue))

                drafts.append((draft_name, date_received, state, tags, missref_generation, stream, auth48, cluster, refs))

            elif event == pulldom.START_ELEMENT and node.tagName == "section":
                name = node.getAttribute('name')
                if name.startswith("IETF"):
                    stream = "ietf"
                elif name.startswith("IAB"):
                    stream = "iab"
                elif name.startswith("IRTF"):
                    stream = "irtf"
                elif name.startswith("INDEPENDENT"):
                    stream = "ise"
                else:
                    stream = None
                    warnings.append("unrecognized section " + name)
        except Exception as e:
            log("Exception when processing an RFC queue entry: %s" % e)
            log("node: %s" % node)
            raise
            
    return drafts, warnings

def update_drafts_from_queue(drafts):
    """Given a list of parsed drafts from the RFC Editor queue, update the
    documents in the database. Return those that were changed."""

    tag_mapping = {
        'IANA': DocTagName.objects.get(slug='iana'),
        'REF':  DocTagName.objects.get(slug='ref')
    }

    slookup = dict((s.slug, s)
                   for s in State.objects.filter(used=True, type=StateType.objects.get(slug="draft-rfceditor")))
    state_mapping = {
        'AUTH': slookup['auth'],
        'AUTH48': slookup['auth48'],
        'AUTH48-DONE': slookup['auth48-done'],
        'EDIT': slookup['edit'],
        'IANA': slookup['iana'],
        'IESG': slookup['iesg'],
        'ISR': slookup['isr'],
        'ISR-AUTH': slookup['isr-auth'],
        'REF': slookup['ref'],
        'RFC-EDITOR': slookup['rfc-edit'],
        'TI': slookup['tooling-issue'],
        'TO': slookup['timeout'],
        'MISSREF': slookup['missref'],
    }

    system = Person.objects.get(name="(System)")

    warnings = []

    names = [t[0] for t in drafts]

    drafts_in_db = dict((d.name, d)
                        for d in Document.objects.filter(type="draft", docalias__name__in=names))

    changed = set()

    for name, date_received, state, tags, missref_generation, stream, auth48, cluster, refs in drafts:
        if name not in drafts_in_db:
            warnings.append("unknown document %s" % name)
            continue

        if not state or state not in state_mapping:
            warnings.append("unknown state '%s' for %s" % (state, name))
            continue

        d = drafts_in_db[name]

        prev_state = d.get_state("draft-rfceditor")
        next_state = state_mapping[state]
        events = []

        # check if we've noted it's been received
        if d.get_state_slug("draft-iesg") == "ann" and not prev_state and not d.latest_event(DocEvent, type="rfc_editor_received_announcement"):
            e = DocEvent(doc=d, rev=d.rev, by=system, type="rfc_editor_received_announcement")
            e.desc = "Announcement was received by RFC Editor"
            e.save()
            send_mail_text(None, "iesg-secretary@ietf.org", None,
                           '%s in RFC Editor queue' % d.name,
                           'The announcement for %s has been received by the RFC Editor.' % d.name)
            # change draft-iesg state to RFC Ed Queue
            prev_iesg_state = State.objects.get(used=True, type="draft-iesg", slug="ann")
            next_iesg_state = State.objects.get(used=True, type="draft-iesg", slug="rfcqueue")

            d.set_state(next_iesg_state)
            e = add_state_change_event(d, system, prev_iesg_state, next_iesg_state)
            if e:
                events.append(e)
            e = update_action_holders(d, prev_iesg_state, next_iesg_state)
            if e:
                events.append(e)
            changed.add(name)
            
        # check draft-rfceditor state
        if prev_state != next_state:
            d.set_state(next_state)

            e = add_state_change_event(d, system, prev_state, next_state)

            if auth48:
                e.desc = re.sub(r"(<b>.*</b>)", "<a href=\"%s\">\\1</a>" % auth48, e.desc)
                e.save()
                # Create or update the auth48 URL whether or not this is a state expected to have one.
                d.documenturl_set.update_or_create(
                    tag_id='auth48',  # look up existing based on this field
                    defaults=dict(url=auth48)  # create or update with this field
                )
            else:
                # Remove any existing auth48 URL when an update does not have one.
                d.documenturl_set.filter(tag_id='auth48').delete()
            if e:
                events.append(e)

            changed.add(name)

        t = DocTagName.objects.filter(slug__in=tags)
        if set(t) != set(d.tags.all()):
            d.tags.clear()
            d.tags.set(t)
            changed.add(name)

        if events:
            d.save_with_history(events)


    # remove tags and states for those not in the queue anymore
    for d in Document.objects.exclude(docalias__name__in=names).filter(states__type="draft-rfceditor").distinct():
        d.tags.remove(*list(tag_mapping.values()))
        d.unset_state("draft-rfceditor")
        # we do not add a history entry here - most likely we already
        # have something that explains what happened
        changed.add(name)

    return changed, warnings


def parse_index(response):
    """Parse RFC Editor index XML into a bunch of tuples."""

    def normalize_std_name(std_name):
        # remove zero padding
        prefix = std_name[:3]
        if prefix in ("RFC", "FYI", "BCP", "STD"):
            try:
                return prefix + str(int(std_name[3:]))
            except ValueError:
                pass
        return std_name

    def extract_doc_list(parentNode, tagName):
        l = []
        for u in parentNode.getElementsByTagName(tagName):
            for d in u.getElementsByTagName("doc-id"):
                l.append(normalize_std_name(d.firstChild.data))
        return l

    also_list = {}
    data = []
    events = pulldom.parse(response)
    for event, node in events:
        try:
            if event == pulldom.START_ELEMENT and node.tagName in ["bcp-entry", "fyi-entry", "std-entry"]:
                events.expandNode(node)
                node.normalize()
                bcpid = normalize_std_name(get_child_text(node, "doc-id"))
                doclist = extract_doc_list(node, "is-also")
                for docid in doclist:
                    if docid in also_list:
                        also_list[docid].append(bcpid)
                    else:
                        also_list[docid] = [bcpid]

            elif event == pulldom.START_ELEMENT and node.tagName == "rfc-entry":
                events.expandNode(node)
                node.normalize()
                rfc_number = int(get_child_text(node, "doc-id")[3:])
                title = get_child_text(node, "title")

                authors = []
                for author in node.getElementsByTagName("author"):
                    authors.append(get_child_text(author, "name"))

                d = node.getElementsByTagName("date")[0]
                year = int(get_child_text(d, "year"))
                month = get_child_text(d, "month")
                month = ["January","February","March","April","May","June","July","August","September","October","November","December"].index(month)+1
                rfc_published_date = datetime.date(year, month, 1)

                current_status = get_child_text(node, "current-status").title()

                updates = extract_doc_list(node, "updates") 
                updated_by = extract_doc_list(node, "updated-by")
                obsoletes = extract_doc_list(node, "obsoletes") 
                obsoleted_by = extract_doc_list(node, "obsoleted-by")
                pages = get_child_text(node, "page-count")
                stream = get_child_text(node, "stream")
                wg = get_child_text(node, "wg_acronym")
                if wg and ((wg == "NON WORKING GROUP") or len(wg) > 15):
                    wg = None

                l = []
                for fmt in node.getElementsByTagName("format"):
                    l.append(get_child_text(fmt, "file-format"))
                file_formats = (",".join(l)).lower()

                abstract = ""
                for abstract in node.getElementsByTagName("abstract"):
                    abstract = get_child_text(abstract, "p")

                draft = get_child_text(node, "draft")
                if draft and re.search(r"-\d\d$", draft):
                    draft = draft[0:-3]

                if len(node.getElementsByTagName("errata-url")) > 0:
                    has_errata = 1
                else:
                    has_errata = 0

                data.append((rfc_number,title,authors,rfc_published_date,current_status,updates,updated_by,obsoletes,obsoleted_by,[],draft,has_errata,stream,wg,file_formats,pages,abstract))
        except Exception as e:
            log("Exception when processing an RFC index entry: %s" % e)
            log("node: %s" % node)
            raise
    for d in data:
        k = "RFC%04d" % d[0]
        if k in also_list:
            d[9].extend(also_list[k])
    return data


def update_docs_from_rfc_index(index_data, errata_data, skip_older_than_date=None):
    """Given parsed data from the RFC Editor index, update the documents in the database

    Yields a list of change descriptions for each document, if any.

    The skip_older_than_date is a bare date, not a datetime.
    """

    errata = {}
    for item in errata_data:
        name = item['doc-id']
        if not name in errata:
            errata[name] = []
        errata[name].append(item)

    std_level_mapping = {
        "Standard": StdLevelName.objects.get(slug="std"),
        "Internet Standard": StdLevelName.objects.get(slug="std"),
        "Draft Standard": StdLevelName.objects.get(slug="ds"),
        "Proposed Standard": StdLevelName.objects.get(slug="ps"),
        "Informational": StdLevelName.objects.get(slug="inf"),
        "Experimental": StdLevelName.objects.get(slug="exp"),
        "Best Current Practice": StdLevelName.objects.get(slug="bcp"),
        "Historic": StdLevelName.objects.get(slug="hist"),
        "Unknown": StdLevelName.objects.get(slug="unkn"),
        }

    stream_mapping = {
        "IETF": StreamName.objects.get(slug="ietf"),
        "INDEPENDENT": StreamName.objects.get(slug="ise"),
        "IRTF": StreamName.objects.get(slug="irtf"),
        "IAB": StreamName.objects.get(slug="iab"),
        "Legacy": StreamName.objects.get(slug="legacy"),
    }

    tag_has_errata = DocTagName.objects.get(slug='errata')
    tag_has_verified_errata = DocTagName.objects.get(slug='verified-errata')
    relationship_obsoletes = DocRelationshipName.objects.get(slug="obs")
    relationship_updates = DocRelationshipName.objects.get(slug="updates")

    system = Person.objects.get(name="(System)")

    for rfc_number, title, authors, rfc_published_date, current_status, updates, updated_by, obsoletes, obsoleted_by, also, draft, has_errata, stream, wg, file_formats, pages, abstract in index_data:

        if skip_older_than_date and rfc_published_date < skip_older_than_date:
            # speed up the process by skipping old entries
            continue

        # we assume two things can happen: we get a new RFC, or an
        # attribute has been updated at the RFC Editor (RFC Editor
        # attributes take precedence over our local attributes)
        events = []
        changes = []
        rfc_published = False

        # make sure we got the document and alias
        doc = None
        name = "rfc%s" % rfc_number
        a = DocAlias.objects.filter(name=name)
        if a:
            doc = a[0].document
        else:
            if draft:
                try:
                    doc = Document.objects.get(name=draft)
                except Document.DoesNotExist:
                    pass

            if not doc:
                changes.append("created document %s" % prettify_std_name(name))
                doc = Document.objects.create(name=name, type=DocTypeName.objects.get(slug="draft"))

            # add alias
            alias, __ = DocAlias.objects.get_or_create(name=name)
            alias.docs.add(doc)
            changes.append("created alias %s" % prettify_std_name(name))

        # check attributes
        if title != doc.title:
            doc.title = title
            changes.append("changed title to '%s'" % doc.title)

        if abstract and abstract != doc.abstract:
            doc.abstract = abstract
            changes.append("changed abstract to '%s'" % doc.abstract)

        if pages and int(pages) != doc.pages:
            doc.pages = int(pages)
            changes.append("changed pages to %s" % doc.pages)

        if std_level_mapping[current_status] != doc.std_level:
            doc.std_level = std_level_mapping[current_status]
            changes.append("changed standardization level to %s" % doc.std_level)

        if doc.get_state_slug() != "rfc":
            doc.set_state(State.objects.get(used=True, type="draft", slug="rfc"))
            move_draft_files_to_archive(doc, doc.rev)
            changes.append("changed state to %s" % doc.get_state())

        if doc.stream != stream_mapping[stream]:
            doc.stream = stream_mapping[stream]
            changes.append("changed stream to %s" % doc.stream)

        if not doc.group: # if we have no group assigned, check if RFC Editor has a suggestion
            if wg:
                doc.group = Group.objects.get(acronym=wg)
                changes.append("set group to %s" % doc.group)
            else:
                doc.group = Group.objects.get(type="individ") # fallback for newly created doc

        if not doc.latest_event(type="published_rfc"):
            e = DocEvent(doc=doc, rev=doc.rev, type="published_rfc")
            # unfortunately, rfc_published_date doesn't include the correct day
            # at the moment because the data only has month/year, so
            # try to deduce it
            #
            # Note: This is in done PST8PDT to preserve compatibility with events created when
            # USE_TZ was False. The published_rfc event was created with a timestamp whose
            # server-local datetime (PST8PDT) matched the publication date from the RFC index.
            # When switching to USE_TZ=True, the timestamps were migrated so they still
            # matched the publication date in PST8PDT. When interpreting the event timestamp
            # as a publication date, you must treat it in the PST8PDT time zone. The
            # RPC_TZINFO constant in ietf.utils.timezone is defined for this purpose.
            d = datetime_from_date(rfc_published_date, RPC_TZINFO)
            synthesized = timezone.now().astimezone(RPC_TZINFO)
            if abs(d - synthesized) > datetime.timedelta(days=60):
                synthesized = d
            else:
                direction = -1 if (d - synthesized).total_seconds() < 0 else +1
                while synthesized.month != d.month or synthesized.year != d.year:
                    synthesized += datetime.timedelta(days=direction)
            e.time = synthesized
            e.by = system
            e.desc = "RFC published"
            e.save()
            events.append(e)

            changes.append("added RFC published event at %s" % e.time.strftime("%Y-%m-%d"))
            rfc_published = True

        for t in ("draft-iesg", "draft-stream-iab", "draft-stream-irtf", "draft-stream-ise"):
            prev_state = doc.get_state(t)
            if prev_state is not None:
                if prev_state.slug not in ("pub", "idexists"):
                    new_state = State.objects.select_related("type").get(used=True, type=t, slug="pub")
                    doc.set_state(new_state)
                    changes.append("changed %s to %s" % (new_state.type.label, new_state))
                    e = update_action_holders(doc, prev_state, new_state)
                    if e:
                        events.append(e)
            elif t == 'draft-iesg':
                doc.set_state(State.objects.get(type_id='draft-iesg', slug='idexists'))

        def parse_relation_list(l):
            res = []
            for x in l:
                if x[:3] in ("NIC", "IEN", "STD", "RTR"):
                    # try translating this to RFCs that we can handle
                    # sensibly; otherwise we'll have to ignore them
                    l = DocAlias.objects.filter(name__startswith="rfc", docs__docalias__name=x.lower())
                else:
                    l = DocAlias.objects.filter(name=x.lower())

                for a in l:
                    if a not in res:
                        res.append(a)
            return res

        for x in parse_relation_list(obsoletes):
            if not RelatedDocument.objects.filter(source=doc, target=x, relationship=relationship_obsoletes):
                r = RelatedDocument.objects.create(source=doc, target=x, relationship=relationship_obsoletes)
                changes.append("created %s relation between %s and %s" % (r.relationship.name.lower(), prettify_std_name(r.source.name), prettify_std_name(r.target.name)))

        for x in parse_relation_list(updates):
            if not RelatedDocument.objects.filter(source=doc, target=x, relationship=relationship_updates):
                r = RelatedDocument.objects.create(source=doc, target=x, relationship=relationship_updates)
                changes.append("created %s relation between %s and %s" % (r.relationship.name.lower(), prettify_std_name(r.source.name), prettify_std_name(r.target.name)))

        if also:
            for a in also:
                a = a.lower()
                if not DocAlias.objects.filter(name=a):
                    DocAlias.objects.create(name=a).docs.add(doc)
                    changes.append("created alias %s" % prettify_std_name(a))

        doc_errata = errata.get('RFC%04d'%rfc_number, [])
        all_rejected = doc_errata and all( er['errata_status_code']=='Rejected' for er in doc_errata )
        if has_errata and not all_rejected:
            if not doc.tags.filter(pk=tag_has_errata.pk).exists():
                doc.tags.add(tag_has_errata)
                changes.append("added Errata tag")
            has_verified_errata = any([ er['errata_status_code']=='Verified' for er in doc_errata ])
            if has_verified_errata and not doc.tags.filter(pk=tag_has_verified_errata.pk).exists():
                doc.tags.add(tag_has_verified_errata)
                changes.append("added Verified Errata tag")
        else:
            if doc.tags.filter(pk=tag_has_errata.pk):
                doc.tags.remove(tag_has_errata)
                if all_rejected:
                    changes.append("removed Errata tag (all errata rejected)")
                else:
                    changes.append("removed Errata tag")
            if doc.tags.filter(pk=tag_has_verified_errata.pk):
                doc.tags.remove(tag_has_verified_errata)
                changes.append("removed Verified Errata tag")

        if changes:
            events.append(DocEvent.objects.create(
                doc=doc,
                rev=doc.rev,
                by=system,
                type="sync_from_rfc_editor",
                desc="Received changes through RFC Editor sync (%s)" % ", ".join(changes),
            ))

            doc.save_with_history(events)

        if changes:
            yield changes, doc, rfc_published


def post_approved_draft(url, name):
    """Post an approved draft to the RFC Editor so they can retrieve
    the data from the Datatracker and start processing it. Returns
    response and error (empty string if no error)."""

    # HTTP basic auth
    username = "dtracksync"
    password = settings.RFC_EDITOR_SYNC_PASSWORD
    headers = {
            "Content-type": "application/x-www-form-urlencoded",
            "Accept": "text/plain",
            "Authorization": "Basic %s" % force_str(base64.encodebytes(smart_bytes("%s:%s" % (username, password)))).replace("\n", ""),
        }

    log("Posting RFC-Editor notification of approved draft '%s' to '%s'" % (name, url))
    text = error = ""

    try:
        r = requests.post(
            url,
            headers=headers,
            data=smart_bytes(urlencode({ 'draft': name })),
            timeout=settings.DEFAULT_REQUESTS_TIMEOUT,
        )

        log("RFC-Editor notification result for draft '%s': %s:'%s'" % (name, r.status_code, r.text))

        if r.status_code != 200:
            raise RuntimeError("Status code is not 200 OK (it's %s)." % r.status_code)

        if force_text(r.text) != "OK":
            raise RuntimeError('Response is not "OK" (it\'s "%s").' % r.text)

    except Exception as e:
        # catch everything so we don't leak exceptions, convert them
        # into string instead
        msg = "Exception on RFC-Editor notification for draft '%s': %s: %s" % (name, type(e), str(e))
        log(msg)
        if settings.SERVER_MODE == 'test':
            debug.say(msg)
        error = str(e)

    return text, error
