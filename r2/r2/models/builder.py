# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
#
# The Original Code is Reddit.
#
# The Original Developer is the Initial Developer.  The Initial Developer of the
# Original Code is CondeNet, Inc.
#
# All portions of the code written by CondeNet are Copyright (c) 2006-2008
# CondeNet, Inc. All Rights Reserved.
################################################################################
from account import *
from link import *
from vote import *
from report import *
from subreddit import SRMember
from listing import Listing
from pylons import i18n, request, g

import subreddit

from r2.lib.wrapped import Wrapped
from r2.lib import utils
from r2.lib.db import operators
from r2.lib.cache import sgm
from r2.lib.comment_tree import link_comments
from r2.lib.menus import CommentSortMenu

from copy import deepcopy, copy

import time
from datetime import datetime,timedelta
from admintools import compute_votes, admintools

EXTRA_FACTOR = 1.5
MAX_RECURSION = 10
DEFAULT_WRAP = Wrapped

class Builder(object):
    def __init__(self, wrap = DEFAULT_WRAP, keep_fn = None):
        self.wrap = wrap
        self.keep_fn = keep_fn

    def keep_item(self, item):
        if self.keep_fn:
            return self.keep_fn(item)
        else:
            return item.keep_item(item)

    def wrap_items(self, items):
        user = c.user if c.user_is_loggedin else None

        #get authors
        #TODO pull the author stuff into add_props for links and
        #comments and messages?
        try:
            aids = set(l.author_id for l in items)
        except AttributeError:
            aids = None

        authors = Account._byID(aids, True) if aids else {}
        # srids = set(l.sr_id for l in items if hasattr(l, "sr_id"))
        subreddits = Subreddit.load_subreddits(items)

        if not user:
            can_ban_set = set()
        else:
            can_ban_set = set(id for (id,sr) in subreddits.iteritems()
                              if sr.can_ban(user))

        #get likes/dislikes
        #TODO Vote.likes should accept empty lists
        likes = Vote.likes(user, items) if user and items else {}
        reports = Report.fastreported(user, items) if user else {}

        uid = user._id if user else None

        # we'll be grabbing this in the spam processing below
        if c.user_is_admin:
            ban_info = admintools.ban_info([x for x in items if x._spam])
        elif user and len(can_ban_set) > 0:
            ban_info = admintools.ban_info(
                [ x for x in items
                  if (x._spam
                      and hasattr(x,'sr_id')
                      and x.sr_id in can_ban_set) ])
        else:
            ban_info = dict()

        types = {}
        wrapped = []
        count = 0
        for item in items:
            w = self.wrap(item)
            wrapped.append(w)

            types.setdefault(w.render_class, []).append(w)

            #TODO pull the author stuff into add_props for links and
            #comments and messages?
            try:
                w.author = authors.get(item.author_id)
                w.friend = item.author_id in user.friends if user else False
            except AttributeError:
                w.author = None
                w.friend = False

            if hasattr(item, "sr_id"):
                w.subreddit = subreddits[item.sr_id]

            vote = likes.get((user, item))
            if vote is not None:
                amount = int(vote._name)
                if amount > 0:
                    w.likes = True
                elif amount < 0:
                    w.likes = False
                else:
                    w.likes = None
            else:
                w.likes = None

            #definite
            w.timesince = utils.timesince(item._date)

            # update vote tallies
            compute_votes(w, item)

            w.score = [w.upvotes, w.downvotes]
            w.deleted = item._deleted

            w.rowstyle = w.rowstyle if hasattr(w,'rowstyle') else ''
            w.rowstyle += ' ' + ('even' if (count % 2) else 'odd')

            count += 1

            # would have called it "reported", but that is already
            # taken on the thing itself as "how many total
            # reports". Indicates whether this user reported this
            # item, and should be visible to all users
            w.report_made = reports.get((user, item, Report._field))

            # if the user can ban things on a given subreddit, or an
            # admin, then allow them to see that the item is spam, and
            # add the other spam-related display attributes
            w.show_reports = False
            w.show_spam    = False
            w.can_ban      = False
            if (c.user_is_admin
                or (user
                    and hasattr(item,'sr_id')
                    and item.sr_id in can_ban_set)):
                w.can_ban = True
                if item._spam:
                    w.show_spam = True
                    if not hasattr(item,'moderator_banned'):
                        w.moderator_banned = False

                    w.autobanned, w.banner = ban_info.get(item._fullname,
                                                              (False, None))

                elif hasattr(item,'reported') and item.reported > 0:
                    w.show_reports = True

        for cls in types.keys():
            cls.add_props(user, types[cls])

        return wrapped

    def get_items(self):
        raise NotImplementedError

    def item_iter(self, *a):
        """Iterates over the items returned by get_items"""
        raise NotImplementedError

    def must_skip(self, item):
        """whether or not to skip any item regardless of whether the builder
        was contructed with skip=true"""
        user = c.user if c.user_is_loggedin else None
        # Meetups are accessed through /meetups.
        if hasattr(item, 'subreddit') and not item.subreddit.can_view(user) and item.subreddit.name != 'meetups':
            return True

class PrecomputedBuilder(Builder):
    def __init__(self, items, **kwargs):
        Builder.__init__(self, **kwargs)
        self.items = items

    def get_items(self):
        items = self.wrap_items(self.items)
        return items, None, None, 0, len(items)

    def item_iter(self, *a):
        return a[0][0]

class QueryBuilder(Builder):
    def __init__(self, query, wrap = DEFAULT_WRAP, keep_fn = None,
                 skip = False, **kw):
        Builder.__init__(self, wrap, keep_fn)
        self.query = query
        self.skip = skip
        self.num = kw.get('num')
        self.start_count = kw.get('count', 0) or 0
        self.after = kw.get('after')
        self.reverse = kw.get('reverse')

        self.prewrap_fn = None
        if hasattr(query, 'prewrap_fn'):
            self.prewrap_fn = query.prewrap_fn
        #self.prewrap_fn = kw.get('prewrap_fn')

    def item_iter(self, a):
        """Iterates over the items returned by get_items"""
        for i in a[0]:
            yield i

    def init_query(self):
        q = self.query

        if self.reverse:
            q._reverse()

        q._data = True
        self.orig_rules = deepcopy(q._rules)
        if self.after:
            q._after(self.after)

    def fetch_more(self, last_item, num_have):
        done = False
        q = self.query
        if self.num:
            num_need = self.num - num_have
            if num_need <= 0:
                #will cause the loop below to break
                return True, None
            else:
                #q = self.query
                #check last_item if we have a num because we may need to iterate
                if last_item:
                    q._rules = deepcopy(self.orig_rules)
                    q._after(last_item)
                    last_item = None
                q._limit = max(int(num_need * EXTRA_FACTOR), 1)
        else:
            done = True
        new_items = list(q)

        return done, new_items

    def get_items(self):
        self.init_query()

        num_have = 0
        done = False
        items = []
        count = self.start_count
        first_item = None
        last_item = None
        have_next = True

        #for prewrap
        orig_items = {}

        #logloop
        self.loopcount = 0

        while not done:
            done, new_items = self.fetch_more(last_item, num_have)

            #log loop
            self.loopcount += 1
            if self.loopcount == 20:
                g.log.debug('BREAKING: %s' % self)
                done = True

            #no results, we're done
            if not new_items:
                break;

            #if fewer results than we wanted, we're done
            elif self.num and len(new_items) < self.num - num_have:
                done = True
                have_next = False

            if not first_item and self.start_count > 0:
                first_item = new_items[0]

            #pre-wrap
            if self.prewrap_fn:
                new_items2 = []
                for i in new_items:
                    new = self.prewrap_fn(i)
                    orig_items[new._id] = i
                    new_items2.append(new)
                new_items = new_items2

            #wrap
            if self.wrap:
                new_items = self.wrap_items(new_items)

            #skip and count
            while new_items and (not self.num or num_have < self.num):
                i = new_items.pop(0)
                count = count - 1 if self.reverse else count + 1
                if not (self.must_skip(i) or self.skip and not self.keep_item(i)):
                    items.append(i)
                    num_have += 1
                if self.wrap:
                    i.num = count
                last_item = i

        #unprewrap the last item
        if self.prewrap_fn and last_item:
            last_item = orig_items[last_item._id]

        if self.reverse:
            items.reverse()
            last_item, first_item = first_item, have_next and last_item
            before_count = count
            after_count = self.start_count - 1
        else:
            last_item = have_next and last_item
            before_count = self.start_count + 1
            after_count = count

        #listing is expecting (things, prev, next, bcount, acount)
        return (items,
                first_item,
                last_item,
                before_count,
                after_count)

class SubredditTagBuilder(QueryBuilder):
    def __init__(self, query, sr_ids, **kw):
        self.sr_ids = sr_ids
        QueryBuilder.__init__(self, query, **kw)

    def keep_item(self, item):
        if item.sr_id not in self.sr_ids:
            return False

        return True

class IDBuilder(QueryBuilder):
    def init_query(self):
        names = self.names = list(tup(self.query))

        if self.reverse:
            names.reverse()

        if self.after:
            try:
                i = names.index(self.after._fullname)
            except ValueError:
                self.names = ()
            else:
                self.names = names[i + 1:]

    def fetch_more(self, last_item, num_have):
        done = False
        names = self.names
        if self.num:
            num_need = self.num - num_have
            if num_need <= 0:
                return True, None
            else:
                if last_item:
                    last_item = None
                slice_size = max(int(num_need * EXTRA_FACTOR), 1)
        else:
            slice_size = len(names)
            done = True

        self.names, new_names = names[slice_size:], names[:slice_size]
        new_items = Thing._by_fullname(new_names, data = True, return_dict=False)

        return done, new_items

class SearchBuilder(QueryBuilder):
    def init_query(self):
        self.skip = True
        self.total_num = 0
        self.start_time = time.time()

        self.start_time = time.time()

    def keep_item(self,item):
        # doesn't use the default keep_item because we want to keep
        # things that were voted on, even if they've chosen to hide
        # them in normal listings
        if item._spam or item._deleted:
            return False
        else:
            return True


    def fetch_more(self, last_item, num_have):
        from r2.lib import solrsearch

        done = False
        limit = None
        if self.num:
            num_need = self.num - num_have
            if num_need <= 0:
                return True, None
            else:
                limit = max(int(num_need * EXTRA_FACTOR), 1)
        else:
            done = True

        search = self.query.run(after = last_item or self.after,
                                reverse = self.reverse,
                                num = limit)

        new_items = Thing._by_fullname(search.docs, data = True, return_dict=False)

        self.total_num = search.hits

        return done, new_items

class CommentBuilderMixin:
    def empty_listing(self, *things):
        parent_name = None
        for t in things:
            try:
                parent_name = t.parent_name
                break
            except AttributeError:
                continue
        l = Listing(None, None, parent_name = parent_name)
        l.things = list(things)
        return Wrapped(l)

class UnbannedCommentBuilder(QueryBuilder):
    def __init__(self, query, sr_ids, **kw):
        self.sr_ids = sr_ids
        QueryBuilder.__init__(self, query, **kw)

    def keep_item(self, item):
        link = Link._byID(item.link_id)
        if link._spam:
            return False

        if item.sr_id not in self.sr_ids:
            return False

        return super(UnbannedCommentBuilder, self).keep_item(item)

class ToplevelCommentBuilder(UnbannedCommentBuilder):
    def keep_item(self, item):
        try:
            item.parent_id
        except AttributeError:
            return True
        else:
            return False

class ContextualCommentBuilder(CommentBuilderMixin, UnbannedCommentBuilder):
    def __init__(self, query, sr_ids, **kw):
        UnbannedCommentBuilder.__init__(self, query, sr_ids, **kw)
        self.sort = CommentSortMenu.operator(CommentSortMenu.default)

    def keep_item(self, item):
        if isinstance(item, Link):
            author = Account._byID(item.author_id)
            if item.subreddit_slow.name == author.draft_sr_name and not c.user == author:
                return False
        if isinstance(item, Comment):
            link = Link._byID(item.link_id)
            if link._spam:
                return False

            if item.sr_id not in self.sr_ids:
                return False

            return super(UnbannedCommentBuilder, self).keep_item(item)
        return True

    def context_from_comment(self, comment):
        if isinstance(comment, Comment):
            link = Link._byID(comment.link_id)
            wrapped, = self.wrap_items((comment,))

            # If there are any child comments, add an expand link
            children = list(Comment._query(Comment.c.parent_id == comment._id))
            if children:
                more = Wrapped(MoreChildren(link, 0))
                more.children.extend(children)
                more.count = len(children)
                wrapped.child = self.empty_listing()
                wrapped.child.things.append(more)

            # If there's a parent comment, surround the child comment with it
            parent_id = getattr(comment, 'parent_id', None)
            if parent_id is not None:
                parent_comment = Comment._byID(parent_id)
                if parent_comment:
                    parent_wrapped, = self.wrap_items((parent_comment,))
                    parent_wrapped.child = self.empty_listing()
                    parent_wrapped.child.things.append(wrapped)
                    wrapped = parent_wrapped

            wrapped.show_response_to = True
        else:
            wrapped, = self.wrap_items((comment,))
        return wrapped

    def get_items(self):
        # call the base implementation, but defer wrapping until later
        old_wrap, self.wrap = self.wrap, None
        things, prev, next, bcount, acount = UnbannedCommentBuilder.get_items(self)
        self.wrap = old_wrap

        things = map(self.context_from_comment, things)
        return things, prev, next, bcount, acount

class CommentBuilder(CommentBuilderMixin, Builder):
    def __init__(self, link, sort, comment = None, context = None, wrap = DEFAULT_WRAP):
        Builder.__init__(self, wrap = wrap)
        self.link = link
        self.comment = comment
        self.context = context

        if sort.col == '_date':
            self.sort_key = lambda x: x._date
        else:
            self.sort_key = lambda x: (getattr(x, sort.col), x._date)
        self.rev_sort = True if isinstance(sort, operators.desc) else False

    def item_iter(self, a):
        for i in a:
            yield i
            if hasattr(i, 'child'):
                for j in self.item_iter(i.child.things):
                    yield j

    def get_items(self, num, nested = True, starting_depth = 0):
        r = link_comments(self.link._id)
        cids, comment_tree, depth, num_children = r
        if cids:
            comment_dict = Comment._byID(cids, data = True, return_dict = True)
        else:
            comment_dict = {}

        #convert tree from lists of IDs into lists of objects
        for pid, cids in comment_tree.iteritems():
            tree = [comment_dict.get(cid) for cid in cids]
            comment_tree[pid] = [c for c in tree if c is not None]

        items = []
        extra = {}
        top = None
        dont_collapse = []
        #loading a portion of the tree
        if isinstance(self.comment, utils.iters):
            candidates = []
            candidates.extend(self.comment)
            dont_collapse.extend(cm._id for cm in self.comment)
            top = self.comment[0]
            #assume the comments all have the same parent
            # TODO: removed by Chris to get rid of parent being sent
            # when morecomments is used.
            #if hasattr(candidates[0], "parent_id"):
            #    parent = comment_dict[candidates[0].parent_id]
            #    items.append(parent)
        #if permalink
        elif self.comment:
            top = self.comment
            dont_collapse.append(top._id)
            #add parents for context
            while self.context > 0 and hasattr(top, 'parent_id'):
                self.context -= 1
                new_top = comment_dict[top.parent_id]
                comment_tree[new_top._id] = [top]
                num_children[new_top._id] = num_children[top._id] + 1
                dont_collapse.append(new_top._id)
                top = new_top
            candidates = [top]
        #else start with the root comments
        else:
            candidates = []
            candidates.extend(comment_tree.get(top, ()))

        #update the starting depth if required
        if top and depth[top._id] > 0:
            delta = depth[top._id]
            for k, v in depth.iteritems():
                depth[k] = v - delta

        def sort_candidates():
            candidates.sort(key = self.sort_key, reverse = self.rev_sort)

        #find the comments
        num_have = 0
        sort_candidates()
        while num_have < num and candidates:
            to_add = candidates.pop(0)
            if to_add._deleted and not comment_tree.has_key(to_add._id):
                pass
            elif depth[to_add._id] < MAX_RECURSION:
                #add children
                if comment_tree.has_key(to_add._id):
                    candidates.extend(comment_tree[to_add._id])
                    sort_candidates()
                items.append(to_add)
                num_have += 1
            else:
                #add the recursion limit
                p_id = to_add.parent_id
                w = Wrapped(MoreRecursion(self.link, 0,
                                          comment_dict[p_id]))
                w.children.append(to_add)
                extra[p_id] = w

        wrapped = self.wrap_items(items)

        cids = dict((cm._id, cm) for cm in wrapped)

        final = []
        #make tree

        for cm in wrapped:
            # don't show spam with no children
            if cm.deleted and not comment_tree.has_key(cm._id):
                continue

            cm.num_children = num_children[cm._id]
            if cm.collapsed and cm._id in dont_collapse:
                cm.collapsed = False
            if cm.collapse_in_link_threads:
                cm.collapsed = True

            parent = cids.get(cm.parent_id) if hasattr(cm, 'parent_id') else None
            if parent:
                if not hasattr(parent, 'child'):
                    parent.child = self.empty_listing()
                parent.child.parent_name = parent._fullname
                parent.child.things.append(cm)
            else:
                final.append(cm)

        #put the extras in the tree
        for p_id, morelink in extra.iteritems():
            parent = cids[p_id]
            parent.child = self.empty_listing(morelink)
            parent.child.parent_name = parent._fullname

        #put the remaining comments into the tree (the show more comments link)
        more_comments = {}
        while candidates:
            to_add = candidates.pop(0)
            direct_child = True
            #ignore top-level comments for now
            if not hasattr(to_add, 'parent_id'):
                p_id = None
            else:
                #find the parent actually being displayed
                #direct_child is whether the comment is 'top-level'
                p_id = to_add.parent_id
                while p_id and not cids.has_key(p_id):
                    p = comment_dict[p_id]
                    if hasattr(p, 'parent_id'):
                        p_id = p.parent_id
                    else:
                        p_id = None
                    direct_child = False

            mc2 = more_comments.get(p_id)
            if not mc2:
                mc2 = MoreChildren(self.link, depth[to_add._id],
                                   parent = comment_dict.get(p_id))
                more_comments[p_id] = mc2
                w_mc2 = Wrapped(mc2)
                if p_id is None:
                    final.append(w_mc2)
                else:
                    parent = cids[p_id]
                    if hasattr(parent, 'child'):
                        parent.child.things.append(w_mc2)
                    else:
                        parent.child = self.empty_listing(w_mc2)
                        parent.child.parent_name = parent._fullname

            #add more children
            if comment_tree.has_key(to_add._id):
                candidates.extend(comment_tree[to_add._id])

            if direct_child:
                mc2.children.append(to_add)

            mc2.count += 1

        return final
