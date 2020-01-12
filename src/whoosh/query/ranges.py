# Copyright 2007 Matt Chaput. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY MATT CHAPUT ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL MATT CHAPUT OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of Matt Chaput.

from __future__ import division
import typing
from typing import Iterable, Tuple

from whoosh import collectors
from whoosh.matching import matchers
from whoosh.query import terms, compound, wrappers, queries
from whoosh.util.times import datetime_to_long

# Typing imports
if typing.TYPE_CHECKING:
    from whoosh import reading, searching


__all__ = ("Range", "TermRange", "NumericRange", "DateRange", "Every")


@collectors.register("range")
class Range(terms.MultiTerm):
    def __init__(self, fieldname, start, end, startexcl=False, endexcl=False,
                 boost=1.0, constantscore=True):
        """
        :param fieldname: The name of the field to search.
        :param start: Match terms equal to or greater than this value.
        :param end: Match terms equal to or less than this value.
        :param startexcl: If True, the range start is exclusive. If False, the
            range start is inclusive.
        :param endexcl: If True, the range end is exclusive. If False, the
            range end is inclusive.
        :param boost: Boost factor that should be applied to the raw score of
            results matched by this query.
        :param constantscore: If True, the compiled query returns a constant
            score (the value of the ``boost`` keyword argument) instead of
            actually scoring the matched terms. This gives a nice speed boost
            and won't affect the results in most cases since ranges
            will almost always be used as a filter.
        """

        super(Range, self).__init__(fieldname, None)
        self.fieldname = fieldname
        self.start = start
        self.end = end
        self.startexcl = startexcl
        self.endexcl = endexcl
        self.boost = boost
        self.constantscore = constantscore

    def __repr__(self):
        return ('%s(%r, %r, %r, %s, %s, boost=%s, constantscore=%s)'
                % (self.__class__.__name__, self.fieldname, self.start,
                   self.end, self.startexcl, self.endexcl, self.boost,
                   self.constantscore))

    def __str__(self):
        startchar = "{" if self.startexcl else "["
        endchar = "}" if self.endexcl else "]"
        start = '' if self.start is None else self.start
        end = '' if self.end is None else self.end
        return u"%s:%s%s TO %s%s" % (self.fieldname, startchar, start, end,
                                     endchar)

    def __eq__(self, other):
        return (
            other and type(self) is type(other) and
            self.fieldname == other.fieldname and
            self.start == other.start and self.end == other.end and
            self.startexcl == other.startexcl and
            self.endexcl == other.endexcl and
            self.boost == other.boost and
            self.constantscore == other.constantscore
        )

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return (hash(self.fieldname) ^ hash(self.start) ^ hash(self.startexcl) ^
                hash(self.end) ^ hash(self.endexcl) ^ hash(self.boost))

    @classmethod
    def combine_collector(cls, collector: 'collectors.Collector',
                          args, kwargs) -> 'collectors.Collector':
        from whoosh import fields

        schema = collector.searcher.schema
        fieldname = args[0]
        field = schema[fieldname]
        if isinstance(field, fields.DateTime):
            qcls = DateRange
        elif isinstance(field, fields.Numeric):
            qcls = NumericRange
        else:
            qcls = TermRange

        return collector.with_query(qcls(*args, **kwargs))

    def is_range(self):
        return True

    def terms(self, reader: 'reading.IndexReader'=None, phrases: bool=True
              ) -> Iterable[Tuple[str, str]]:
        return self.simplify(reader).terms(reader, phrases)

    def _comparable_start(self):
        if self.start is None:
            return queries.Lowest, 0
        else:
            second = 1 if self.startexcl else 0
            return self.start, second

    def _comparable_end(self):
        if self.end is None:
            return queries.Highest, 0
        else:
            second = -1 if self.endexcl else 0
            return self.end, second

    def normalize(self):
        from whoosh.query import Every

        if self.start in ('', None) and self.end in (u'\uffff', None):
            eq = Every(self.fieldname, boost=self.boost)
            eq.set_extent(self.startchar, self.endchar)
            return eq

        if self.start == self.end and (self.startexcl or self.endexcl):
            return queries.NullQuery().set_extent(self.startchar, self.endchar)

        return self

    def overlaps(self, other):
        if not isinstance(other, self.__class__):
            return False
        if self.field() != other.field():
            return False

        start1 = self._comparable_start()
        start2 = other._comparable_start()
        end1 = self._comparable_end()
        end2 = other._comparable_end()

        return ((start2 <= start1 <= end2) or
                (start2 <= end1 <= end2) or
                (start1 <= start2 <= end1) or
                (start1 <= end2 <= end1))

    def can_merge_with(self, other):
        return self.overlaps(other)

    def merge(self, other, intersect=True):
        assert self.fieldname == other.fieldname

        start1 = self._comparable_start()
        start2 = other._comparable_start()
        end1 = self._comparable_end()
        end2 = other._comparable_end()

        if start1 >= start2 and end1 <= end2:
            start = start2
            end = end2
        elif start2 >= start1 and end2 <= end1:
            start = start1
            end = end1
        elif intersect:
            start = max(start1, start2)
            end = min(end1, end2)
        else:
            start = min(start1, start2)
            end = max(end1, end2)

        startval = None if start[0] is queries.Lowest else start[0]
        startexcl = start[1] == 1
        endval = None if end[0] is queries.Highest else end[0]
        endexcl = end[1] == -1

        boost = max(self.boost, other.boost)
        constantscore = self.constantscore or other.constantscore

        return self.__class__(self.fieldname, startval, endval, startexcl,
                              endexcl, boost=boost,
                              constantscore=constantscore)

    def specialize(self, schema):
        from whoosh import fields

        fieldname = self.field()
        if fieldname in schema:
            field = schema[fieldname]

            if self.start is None and self.end is None:
                q = Every(fieldname, boost=self.boost)
            elif isinstance(self.start, bytes) or isinstance(self.end, bytes):
                q = TermRange(fieldname, self.start, self.end, self.startexcl,
                              self.endexcl, self.boost, self.constantscore)
            elif isinstance(field, fields.DateTime):
                q = DateRange(fieldname, self.start, self.end, self.startexcl,
                              self.endexcl, self.boost, self.constantscore)
            elif isinstance(field, fields.Numeric):
                q = NumericRange(fieldname, self.start, self.end, self.startexcl,
                                 self.endexcl, self.boost, self.constantscore)
            else:
                q = TermRange(fieldname, self.start, self.end, self.startexcl,
                              self.endexcl, self.boost, self.constantscore)
            return q
        else:
            return self

    def matcher(self, searcher, context=None):
        # The default implementation looks at the field type and picks the
        # appropriate subclass to generate the matcher

        fieldname = self.field()
        schema = searcher.schema
        if not fieldname in schema:
            return matchers.NullMatcher()

        q = self.specialize(schema)
        return q.matcher(searcher, context)


@collectors.register("term_range")
class TermRange(Range):
    """Matches documents containing any terms in a given range.

    >>> # Match documents where the indexed "id" field is greater than or equal
    >>> # to 'apple' and less than or equal to 'pear'.
    >>> TermRange("id", u"apple", u"pear")
    """

    def __eq__(self, other):
        return (
            type(other) is TermRange and
            self.field() == other.field() and
            self.start == other.start and
            self.end == other.end and
            self.startexcl == other.startexcl and
            self.endexcl == other.endexcl and
            self.boost == other.boost and
            self.constantscore == other.constantscore
        )

    def __hash__(self):
        return (
            hash(type(self)) ^
            hash(self.field()) ^
            hash(self.start) ^
            hash(self.end) ^
            hash(self.startexcl) ^
            hash(self.endexcl) ^
            hash(self.boost) ^
            hash(self.constantscore)
        )

    def normalize(self):
        from whoosh.query import Every

        if self.start in ('', None) and self.end in (u'\uffff', None):
            eq = Every(self.fieldname, boost=self.boost)
            return eq.set_extent(self.startchar, self.endchar)

        if self.start == self.end:
            if self.startexcl or self.endexcl:
                nq = queries.NullQuery()
                return nq.set_extent(self.startchar, self.endchar)
            else:
                tq = terms.Term(self.fieldname, self.start, boost=self.boost)
                return tq.set_extent(self.startchar, self.endchar)
        else:
            return TermRange(
                self.fieldname, self.start, self.end, self.startexcl,
                self.endexcl, boost=self.boost, constantscore=self.constantscore
            ).set_extent(self.startchar, self.endchar)

    #def replace(self, fieldname, oldtext, newtext):
    #    q = self.copy()
    #    if q.fieldname == fieldname:
    #        if q.start == oldtext:
    #            q.start = newtext
    #        if q.end == oldtext:
    #            q.end = newtext
    #    return q

    def _btexts(self, ixreader: 'reading.IndexReader') -> Iterable[bytes]:
        fieldname = self.fieldname
        field = ixreader.schema[fieldname]
        startexcl = self.startexcl
        endexcl = self.endexcl

        start = self.start
        if start is None:
            start = b""
        elif not isinstance(start, bytes):
            try:
                start = field.to_bytes(start)
            except ValueError:
                return

        end = self.end
        if end is not None and not isinstance(end, bytes):
            try:
                end = field.to_bytes(end)
            except ValueError:
                return

        # We call term_range with end=None here and manually check the end,
        # because if you give term_range an end term it yields terms up to but
        # not including the end term
        for termbytes in ixreader.term_range(fieldname, start, None):
            if startexcl and termbytes == start:
                continue
            if endexcl and termbytes == end:
                break
            if end is not None and termbytes > end:
                break
            yield termbytes

    def matcher(self, searcher, context=None):
        return terms.MultiTerm.matcher(self, searcher, context)


class NumericRange(Range):
    """
    A range query for NUMERIC fields. Takes advantage of tiered indexing
    to speed up large ranges by matching at a high resolution at the edges of
    the range and a low resolution in the middle.

    >>> # Match numbers from 10 to 5925 in the "number" field.
    >>> nr = NumericRange("number", 10, 5925)
    """

    def estimate_size(self, ixreader):
        return self.simplify(ixreader).estimate_size(ixreader)

    def estimate_min_size(self, ixreader):
        return self.simplify(ixreader).estimate_min_size(ixreader)

    def docs(self, searcher: 'searching.Searcher',
             deleting: bool=False) -> Iterable[int]:
        reader = searcher.reader()
        return self.simplify(reader).docs(searcher, deleting)

    def simplify(self, ixreader: 'reading.IndexReader') -> queries.Query:
        from whoosh.fields import Numeric
        from whoosh.util.numeric import split_ranges, to_sortable

        if isinstance(self.start, bytes) or isinstance(self.end, bytes):
            raise ValueError("NumericRange should not contain bytes")

        field = ixreader.schema[self.fieldname]
        if not isinstance(field, Numeric):
            raise Exception("NumericRange: field %r is not numeric"
                            % self.fieldname)
        numtype = field.numtype
        intsize = field.bits
        signed = field.signed

        start = self.start
        if start is None:
            start = 0
        else:
            start = field.prepare_number(start)
            start = to_sortable(numtype, intsize, signed, start)
            if self.startexcl:
                start += 1

        end = self.end
        if end is None:
            end = 2 ** field.bits - 1
        else:
            end = field.prepare_number(end)
            end = to_sortable(numtype, intsize, signed, end)
            if self.endexcl:
                end -= 1

        subqueries = []
        stb = field.sortable_to_bytes
        # Get the term ranges for the different resolutions
        if field.shift_step:
            # Iterator of (range_start, range_end, shift) tuples
            ranges = split_ranges(intsize, field.shift_step, start, end)
        else:
            ranges = [(start, end, 0)]

        for startnum, endnum, shift in ranges:
            if startnum == endnum:
                subq = terms.Term(self.fieldname, stb(startnum, shift))
            else:
                startbytes = stb(startnum, shift)
                endbytes = stb(endnum, shift)
                subq = TermRange(self.fieldname, startbytes, endbytes)
            subqueries.append(subq)

        if len(subqueries) == 1:
            q = subqueries[0]
        elif subqueries:
            q = compound.Or(subqueries, boost=self.boost)
        else:
            return queries.NullQuery()

        if self.constantscore:
            q = wrappers.ConstantScoreQuery(q, self.boost)
        return q

    def matcher(self, searcher, context=None):
        q = self.simplify(searcher.reader())
        return q.matcher(searcher, context)


class DateRange(NumericRange):
    """
    This is a very thin subclass of :class:`NumericRange` that only
    overrides the initializer and ``__repr__()`` methods to work with datetime
    objects instead of numbers. Internally this object converts the datetime
    objects it's created with to numbers and otherwise acts like a
    ``NumericRange`` query.

    >>> DateRange("date", datetime(2010, 11, 3, 3, 0),
    ...           datetime(2010, 11, 3, 17, 59))
    """

    def __init__(self, fieldname, start, end, startexcl=False, endexcl=False,
                 boost=1.0, constantscore=True):
        self.startdate = start
        self.enddate = end
        if start:
            start = datetime_to_long(start)
        if end:
            end = datetime_to_long(end)
        super(DateRange, self).__init__(fieldname, start, end,
                                        startexcl=startexcl, endexcl=endexcl,
                                        boost=boost,
                                        constantscore=constantscore)

    def __repr__(self):
        return '%s(%r, %r, %r, %s, %s, boost=%s)' % (
            self.__class__.__name__,
            self.fieldname,
            self.startdate, self.enddate,
            self.startexcl, self.endexcl,
            self.boost
        )


@collectors.register("all")
class Every(queries.Query):
    """
    A query that matches every document, or every document that has a term in
    a given field.

    This is VERY inefficient. Instead of using this to match all documents that
    contain a term from a given field, you should add an identifying term to
    those documents when you index them.
    """

    def __init__(self, fieldname: str=None, startchar: int=None,
                 endchar: int=None, error: str=None, boost=1.0):
        super(Every, self).__init__(startchar=startchar, endchar=endchar,
                                    error=error, boost=boost)
        self.fieldname = fieldname

    def __repr__(self):
        return "<%s %r>" % (type(self).__name__, self.fieldname)

    def __eq__(self, other: 'Every') -> bool:
        if type(self) == type(other):
            if self.is_total() and other.is_total():
                return True
            else:
                if self.fieldname != other.fieldname:
                    return False
                if self.boost != other.boost:
                    return False
                return True
        return False

    def __ne__(self, other: 'Every') -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        h = hash(type(self))
        if not self.is_total():
            h ^= hash(self.fieldname)
        h ^= hash(self.boost)
        return h

    def is_total(self) -> bool:
        return self.fieldname in (None, "", "*")

    def estimate_size(self, reader: 'reading.IndexReader'):
        return reader.doc_count()

    def matcher(self, searcher: 'searching.Searcher',
                context: 'searching.SearchContext') -> 'matchers.Matcher':
        reader = searcher.reader()
        include = context.include if context else None
        exclude = context.exclude if context else None

        if self.is_total():
            include = searcher.to_comb(include)
            exclude = searcher.to_comb(exclude)
            matcher = matchers.IteratorMatcher(reader.all_doc_ids(),
                                               include=include, exclude=exclude)
        else:
            # This is a hacky hack, but just create an in-memory set of all the
            # document numbers of every term in the field. This is SLOOOW for
            # large indexes
            docset = set()
            for text in reader.lexicon(self.fieldname):
                pr = searcher.matcher(self.fieldname, text, include=include,
                                      exclude=exclude)
                docset.update(pr.all_ids())
            matcher = matchers.ListMatcher(sorted(docset))

        return matcher
