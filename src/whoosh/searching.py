
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

"""
This module contains classes and functions related to searching the index.
"""


from __future__ import division

import copy
import weakref
import typing
from abc import abstractmethod, abstractproperty
from typing import Optional, Sequence, Tuple, Union, Dict, Any, Iterable, Set

from whoosh import index
from whoosh.idsets import DocIdSet, BitSet
from whoosh.query import queries
from whoosh.matching import matchers

# Typing imports
if typing.TYPE_CHECKING:
    from whoosh import collectors, fields, reading, results, scoring, sorting


# Typing aliases

TermText = Union[str, bytes]
ConditionerType = 'Calllable[[Searcher, int, SearchContext], SearchContext]'
FilterType = (
    "Union[collectors.Collector, results.Results, queries.Query, "
    "idsets.DocIdSet, Set[int]]"
)


# Search context object

class SearchContext:
    """
    A container for information about the current search that may be used
    by the collector or the query objects to change how they operate.
    """

    def __init__(self, weighting: 'scoring.WeightingModel'=None,
                 top_searcher: 'SearcherType'=None,
                 top_query: 'queries.Query'=None,
                 offset: int=0,
                 limit: Optional[int]=0,
                 optimize: bool=True,
                 include: FilterType=None,
                 exclude: FilterType=None):
        """
        :param weighting: the Weighting object to use for scoring documents.
        :param top_searcher: a reference to the top-level Searcher object.
        :param top_query: a reference to the top-level query object.
        :param limit: the number of results requested by the user.
        :param optimize: whether to use block quality optimizations.
        :param include: a doc ID set or query representing documents that are
            allowed in the search results.
        :param exclude: a doc ID set or query representing documents not allowed
            in the search results.
        """

        self.weighting = weighting
        self.top_searcher = top_searcher
        self.top_query = top_query
        self.offset = offset
        self.limit = limit
        self.optimize = optimize
        self.include = include
        self.exclude = exclude
        self.query_local_data = {}

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.__dict__)

    def copy(self):
        return copy.deepcopy(self)

    @classmethod
    def boolean(cls) -> 'SearchContext':
        return cls(weighting=None)

    def to_boolean(self) -> 'SearchContext':
        return self.set(weighting=None)

    @property
    def scored(self) -> bool:
        return self.weighting is not None

    def set(self, **kwargs):
        ctx = copy.copy(self)
        ctx.query_local_data = self.query_local_data.copy()

        if "query_local_data" in kwargs:
            ctx.query_local_data.update(kwargs.pop("query_local_data", None))

        ctx.__dict__.update(kwargs)
        return ctx


def setup_weighting(weighting: 'scoring.WeightingModel'
                    ) -> 'scoring.WeightingModel':
    from whoosh.scoring import BM25F

    weighting = weighting or BM25F()
    if isinstance(weighting, type):
        weighting = weighting()
    return weighting


# Searcher base class

class SearcherType:
    def __init__(self, weighting: 'scoring.WeightingModel'=None):
        self.idf_cache = {}
        # Cache for PostingCategorizer objects (supports fields without columns)
        self.field_caches = {}
        # Cache for docnum filters
        self.filter_cache = {}

        self._weighting = setup_weighting(weighting)
        self._closereader = True
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @classmethod
    def from_reader(cls, reader: 'reading.IndexReader', **kwargs):
        subcls = Searcher if reader.is_atomic() else MultiSearcher
        return subcls(reader, **kwargs)

    @classmethod
    def from_reader_list(cls, readers: 'Sequence[reading.IndexReader]',
                         **kwargs):
        from whoosh.reading import leaf_readers, MultiReader

        rs = leaf_readers(readers)
        if len(rs) == 1:
            return Searcher(rs[0], **kwargs)
        else:
            mr = MultiReader(rs)
            return MultiSearcher(mr, **kwargs)

    # Interface

    @abstractproperty
    def schema(self) -> 'fields.Schema':
        raise NotImplementedError

    @abstractproperty
    def weighting(self) -> 'scoring.WeightingModel':
        raise NotImplementedError

    @abstractmethod
    def leaf_searchers(self) -> 'Sequence[Tuple[Searcher, int]]':
        """
        Returns a sequence of ``(Searcher, doc_offset)`` tuples representing
        the leaf searchers and document offsets. If this searcher is atomic, it
        returns a list of itself. If it's a parent, it returns a list of its
        sub-searchers.
        """

        raise NotImplementedError

    def is_atomic(self) -> bool:
        """
        Returns True if this searcher is not a parent to sub-searchers.
        """

        return True

    def parent(self) -> 'SearcherType':
        """
        Returns the parent of this searcher (if has_parent() is True), or
        else self.
        """

        return self

    def doc_count(self) -> int:
        """
        Returns the number of UNDELETED documents in the index.
        """

        return self.reader().doc_count()

    def doc_count_all(self) -> int:
        """
        Returns the total number of documents, DELETED OR UNDELETED, in
        the index.
        """

        return self.reader().doc_count_all()

    def field_length(self, fieldname: str) -> int:
        return self.reader().field_length(fieldname)

    def min_field_length(self, fieldname: str) -> int:
        return self.reader().min_field_length(fieldname)

    def max_field_length(self, fieldname: str) -> int:
        return self.reader().max_field_length(fieldname)

    def avg_field_length(self, fieldname, default=None):
        if not self.schema[fieldname].scorable:
            return default
        return self.field_length(fieldname) / (self.doc_count_all() or 1)

    def doc_field_length(self, docnum: int, fieldname: str, default: int=1
                         ) -> int:
        return self.reader().doc_field_length(docnum, fieldname, default)

    def stored_fields(self, docnum: int) -> Dict[str, Any]:
        return self.reader().stored_fields(docnum)

    @abstractmethod
    def reader(self) -> 'reading.IndexReader':
        """
        Returns the underlying :class:`~whoosh.reading.IndexReader`.
        """

        raise NotImplementedError

    def matcher(self, fieldname: str, text: TermText,
                weighting: 'scoring.WeightingModel' = None,
                include: 'queries.Query' = None,
                exclude: 'queries.Query' = None,
                qf: int = 1) -> 'matchers.Matcher':
        weighting = weighting or self.weighting
        scorer = weighting.scorer(self, fieldname, text, qf=qf)
        include = self.to_comb(include)
        exclude = self.to_comb(exclude)
        return self.reader().matcher(fieldname, text, scorer=scorer,
                                     include=include, exclude=exclude)

    def index(self) -> 'Optional[index.Index]':
        return None

    def up_to_date(self):
        """
        Returns True if this Searcher represents the latest version of the
        index, for backends that support versioning.
        """

        ix = self.index()
        if not ix:
            raise Exception("The searcher has no reference to its index")
        return self.reader().generation() == ix.latest_generation()

    def refresh(self):
        """
        Returns a fresh searcher for the latest version of the index::

            my_searcher = my_searcher.refresh()

        If the index has not changed since this searcher was created, this
        searcher is simply returned.

        This method may CLOSE underlying resources that are no longer needed
        by the refreshed searcher, so you CANNOT continue to use the original
        searcher after calling ``refresh()`` on it.
        """

        ix = self.index()
        if ix is None:
            raise Exception("This searcher has no reference to its index")

        if self.up_to_date():
            return self

        # Get a new reader, re-using resources from the current reader if
        # possible
        self.closed = True
        newreader = ix.reader(reuse=self.reader())
        return SearcherType.from_reader(newreader, fromindex=ix,
                                        weighting=self.weighting)

    def vector(self, docid: int, fieldname: str):
        return self.reader().vector(docid, fieldname)

    def close(self):
        if self._closereader:
            self.reader().close()
        self.closed = True

    # Derived and helper methods

    def idf(self, fieldname: str, termbytes: TermText) -> float:
        """
        Calculates the Inverse Document Frequency of the current term (calls
        idf() on the searcher's default Weighting object).

        :param fieldname: the field containing the term.
        :param termbytes: the term to get the IDF for.
        """

        cache = self.idf_cache
        field = self.schema[fieldname]
        if not isinstance(termbytes, bytes):
            termbytes = field.to_bytes(termbytes)

        term = (fieldname, termbytes)
        try:
            return cache[term]
        except KeyError:
            cache[term] = idf = self.weighting.idf(self, fieldname, termbytes)
            return idf

    def context(self, weighting: 'scoring.WeightingModel' = None,
                top_query: 'queries.Query' = None, limit: int=0,
                offset: int=0):
        """
        Returns a ``SearchContext`` object

        :param weighting: the WeightingModel object to use for scoring
            documents.
        :param top_query: a reference to the top-level query object.
        :param limit: the number of results requested by the user.
        """

        weighting = weighting or self.weighting
        return SearchContext(weighting=weighting, top_query=top_query,
                             offset=offset, limit=limit)

    def boolean_context(self) -> SearchContext:
        """
        Shortcut returns a SearchContext set for unscored (boolean)
        searching.
        """

        return self.context(weighting=None)

    def document(self, **kw) -> Dict[str, Any]:
        """
        Convenience method returns the stored fields of a document
        matching the given keyword arguments, where the keyword keys are
        field names and the values are terms that must appear in the field.

        This method is equivalent to::

            searcher.stored_fields(searcher.document_number(<keyword args>))

        Where Searcher.documents() returns a generator, this function returns
        either a dictionary or None. Use it when you assume the given keyword
        arguments either match zero or one documents (i.e. at least one of the
        fields is a unique key).

        >>> stored_fields = searcher.document(path=u"/a/b")
        >>> if stored_fields:
        ...   print(stored_fields['title'])
        ... else:
        ...   print("There is no document with the path /a/b")

        :param kw: keyword arguments map field names to terms to search for in
            that field.
        """

        for p in self.documents(**kw):
            return p

    def documents(self, **kw) -> Iterable[Dict[str, Any]]:
        """
        Convenience method returns the stored fields of a document
        matching the given keyword arguments, where the keyword keys are field
        names and the values are terms that must appear in the field.

        Returns a generator of dictionaries containing the stored fields of any
        documents matching the keyword arguments. If you do not specify any
        arguments (``Searcher.documents()``), this method will yield **all**
        documents.

        >>> for stored_fields in searcher.documents(emailto=u"matt@whoosh.ca"):
        ...   print("Email subject:", stored_fields['subject'])

        :param kw: keyword arguments map field names to terms to search for in
            that field.
        """

        reader = self.reader()
        return (reader.stored_fields(docnum) for docnum
                in self.document_numbers(**kw))

    def _kws_to_query(self, kw: Dict[str, Any]) -> 'queries.Query':
        # Converts a keyword dict to a query
        from whoosh.query import And, Every, Term

        # Convert the values in the dict to bytes
        for k, v in kw.items():
            if "__" in k:
                fieldname, op = k.split("__", 1)
            else:
                fieldname = k
                op = "contains"

            fieldobj = self.schema[k]
            if not isinstance(v, bytes):
                v = fieldobj.to_bytes(v)
            kw[k] = v

        # Make Term queries for each value
        subqueries = []
        for key, value in kw.items():
            subqueries.append(Term(key, value))

        # Make an And query from the terms
        if subqueries:
            q = And(subqueries).normalize()
        else:
            q = Every()
        return q

    def document_number(self, **kw) -> int:
        """
        Returns the document number of the document matching the given
        keyword arguments, where the keyword keys are field names and the
        values are terms that must appear in the field.

        >>> docnum = searcher.document_number(path=u"/a/b")

        Where Searcher.document_numbers() returns a generator, this function
        returns either an int or None. Use it when you assume the given keyword
        arguments either match zero or one documents (i.e. at least one of the
        fields is a unique key).

        :param kw: keyword arguments map field names to terms to search for in
            that field.
        """

        for docnum in self.document_numbers(**kw):
            return docnum

    def document_numbers(self, **kw) -> Iterable[int]:
        """
        Returns a generator of the document numbers for documents matching
        the given keyword arguments, where the keyword keys are field names and
        the values are terms that must appear in the field. If you do not
        specify any arguments (``Searcher.document_numbers()``), this method
        will yield **all** document numbers.

        >>> docnums = list(searcher.document_numbers(emailto="matt@whoosh.ca"))

        :param kw: keyword arguments map field names to terms to search for in
            that field.
        """

        q = self._kws_to_query(kw)
        return self.docs_for_query(q)

    def hit(self, **kw):
        results = self.hits(**kw)
        return results[0]

    def hits(self, **kw):
        q = self._kws_to_query(kw)
        return self.search(q, limit=1)

    def _find_unique(self, uniques: Iterable[Tuple[str, bytes]]) -> Set:
        # uniques is a list of ("unique_field_name", "field_value") tuples
        delset = set()
        for name, value in uniques:
            docnum = self.document_number(**{name: value})
            if docnum is not None:
                delset.add(docnum)
        return delset

    def to_comb(self, obj: FilterType) -> Optional[DocIdSet]:
        from whoosh.collectors import Collector
        from whoosh.results import Results

        if obj is None:
            return None

        if isinstance(obj, Collector):
            obj = obj.query()

        if isinstance(obj, Results):
            obj = obj.docs()
        elif isinstance(obj, queries.Query):
            # TODO: cache this
            obj = BitSet(self.docs_for_query(obj),
                         size=self.doc_count_all())

        return obj

    def suggest(self, fieldname: str, text: TermText, limit: int=5,
                maxdist: int=2, prefix: int=0) -> Sequence[str]:
        """
        Returns a sorted list of suggested corrections for the given
        mis-typed word ``text`` based on the contents of the given field::

            >>> searcher.suggest("content", "specail")
            ["special"]

        This is a convenience method. If you are planning to get suggestions
        for multiple words in the same field, it is more efficient to get a
        :class:`~whoosh.spelling.Corrector` object and use it directly::

            corrector = searcher.corrector("fieldname")
            for word in words:
                print(corrector.suggest(word))

        :param fieldname: the field to provide the suggestions.
        :param text: the word to correct.
        :param limit: only return up to this many suggestions. If there are not
            enough terms in the field within ``maxdist`` of the given word, the
            returned list will be shorter than this number.
        :param maxdist: the largest edit distance from the given word to look
            at. Numbers higher than 2 are not very effective or efficient.
        :param prefix: require suggestions to share a prefix of this length
            with the given word. This is often justifiable since most
            misspellings do not involve the first letter of the word. Using a
            prefix dramatically decreases the time it takes to generate the
            list of words.
        """

        c = self.reader().corrector(fieldname)
        return c.suggest(text, limit=limit, maxdist=maxdist, prefix=prefix)

    def search_page(self, query: 'queries.Query', pagenum: int,
                    pagelen: int = 10,
                    **kwargs) -> 'results.ResultsPage':
        """
        This method is Like the :meth:`Searcher.search` method, but returns
        a :class:`ResultsPage` object. This is a convenience function for
        getting a certain "page" of the results for the given query, which is
        often useful in web search interfaces.

        For example::

            querystring = request.get("q")
            query = queryparser.parse("content", querystring)

            pagenum = int(request.get("page", 1))
            pagelen = int(request.get("perpage", 10))

            results = searcher.search_page(query, pagenum, pagelen=pagelen)
            print("Page %d of %d" % (results.pagenum, results.pagecount))
            print("Showing results %d-%d of %d"
                  % (results.offset + 1, results.offset + results.pagelen + 1,
                     len(results)))
            for hit in results:
                print("%d: %s" % (hit.rank + 1, hit["title"]))

        (Note that results.pagelen might be less than the pagelen argument if
        there aren't enough results to fill a page.)

        Any additional keyword arguments you supply are passed through to
        :meth:`Searcher.search`. For example, you can get paged results of a
        sorted search::

            results = searcher.search_page(q, 2, sortedby="date", reverse=True)

        Currently, searching for page 100 with pagelen of 10 takes the same
        amount of time as using :meth:`Searcher.search` to find the first 1000
        results. That is, this method does not have any special optimizations
        or efficiencies for getting a page from the middle of the full results
        list. (A future enhancement may allow using previous page results to
        improve the efficiency of finding the next page.)

        This method will raise a ``ValueError`` if you ask for a page number
        higher than the number of pages in the resulting query.

        :param query: the :class:`whoosh.query.Query` object to match.
        :param pagenum: the page number to retrieve, starting at ``1`` for the
            first page.
        :param pagelen: the number of results per page.
        """

        from whoosh import results

        if pagenum < 1:
            raise ValueError("pagenum must be >= 1")

        r = self.search(query, limit=pagenum * pagelen, **kwargs)
        return results.ResultsPage(r, pagenum, pagelen)

    def find(self, defaultfield, querystring, **kwargs):
        from whoosh.parsing import QueryParser
        qp = QueryParser(defaultfield, schema=self.schema)
        q = qp.parse(querystring)
        return self.search(q, **kwargs)

    def docs_for_query(self, q, for_deletion=False):
        """
        Returns an iterator of document numbers for documents matching the
        given :class:`whoosh.query.Query` object.
        """

        if self.is_atomic():
            for docnum in q.docs(self, deleting=for_deletion):
                yield docnum
        else:
            for s, offset in self.leaf_searchers():
                for docnum in q.docs(s, deleting=for_deletion):
                    yield docnum + offset

    @property
    def q(self):
        from whoosh.collectors import Collector
        from whoosh.query import NullQuery

        return Collector(self, NullQuery())

    def search(self, q: 'queries.Query', limit: Optional[int]=10,
               sortedby: 'sorting.FacetType' = None, reverse: bool = False,
               groupedby: 'sorting.FacetType' = None,
               collapse: 'sorting.FacetType' = None,
               collapse_limit: int = 1,
               collapse_order: 'sorting.FacetType' = None,
               optimize: bool = True, filter=None, mask=None,
               terms: bool = False,
               maptype=None, scored: bool = True, spans: bool = False
               ) -> 'results.Results':
        """
        Runs a :class:`whoosh.query.Query` object on this searcher and
        returns a :class:`Results` object. See :doc:`/searching` for more
        information.

        This method takes many keyword arguments (documented below).

        See :doc:`/facets` for information on using ``sortedby`` and/or
        ``groupedby``. See :ref:`collapsing` for more information on using
        ``collapse``, ``collapse_limit``, and ``collapse_order``.

        :param q: a :class:`whoosh.query.Query` object to use to match
            documents.
        :param limit: the maximum number of documents to score. If you're only
            interested in the top N documents, you can set limit=N to limit the
            scoring for a faster search. Default is 10.
        :param scored: whether to score the results. Overriden by ``sortedby``.
            If both ``scored=False`` and ``sortedby=None``, the results will be
            in arbitrary order, but will usually be computed faster than
            scored or sorted results.
        :param sortedby: see :doc:`/facets`.
        :param reverse: Reverses the direction of the sort. Default is False.
        :param groupedby: see :doc:`/facets`.
        :param optimize: use optimizations to get faster results when possible.
            Default is True.
        :param filter: a query, Results object, or set of docnums. The results
            will only contain documents that are also in the filter object.
        :param mask: a query, Results object, or set of docnums. The results
            will not contain any documents that are in the mask object.
        :param terms: if True, record which terms were found in each matching
            document. See :doc:`/searching` for more information. Default is
            False.
        :param maptype: by default, the results of faceting with ``groupedby``
            is a dictionary mapping group names to ordered lists of document
            numbers in the group. You can pass a
            :class:`whoosh.sorting.FacetMap` subclass to this keyword argument
            to specify a different (usually faster) method for grouping. For
            example, ``maptype=sorting.Count`` would store only the count of
            documents in each group, instead of the full list of document IDs.
        :param collapse: a :doc:`facet </facets>` to use to collapse the
            results. See :ref:`collapsing` for more information.
        :param collapse_limit: the maximum number of documents to allow with
            the same collapse key. See :ref:`collapsing` for more information.
        :param collapse_order: an optional ordering :doc:`facet </facets>`
            to control which documents are kept when collapsing. The default
            (``collapse_order=None``) uses the results order (e.g. the highest
            scoring documents in a scored search).

        """

        from whoosh import collectors

        col = collectors.Collector(self, q)

        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")

        if terms:
            col = col.with_terms()
        if spans:
            col = col.with_spans()

        if sortedby:
            col = col.sorted_by(sortedby)
        if groupedby:
            col = col.grouped_by(groupedby, maptype=maptype)

        if reverse:
            col = col.reversed()

        if sortedby or groupedby:
            col = col.limit(limit)
        else:
            col = col.top(limit)

        if collapse:
            col = col.collapse(collapse, limit=collapse_limit,
                               order=collapse_order)

        weighting = self.weighting if scored else None
        context = SearchContext(weighting=weighting, top_searcher=self,
                                top_query=q, limit=limit, optimize=optimize,
                                include=filter, exclude=mask)
        return col.results(context)

    def correct_query(self, q: 'queries.Query', qstring: str,
                      correctors: 'Dict[str, spelling.Corrector]' = None,
                      terms: 'Sequence[Tuple[str, TermText]]' = None,
                      maxdist: int = 2, prefix: int = 0):
        """
        Returns a corrected version of the given user query using a default
        :class:`whoosh.spelling.ReaderCorrector`.

        The default:

        * Corrects any words that don't appear in the index.

        * Takes suggestions from the words in the index. To make certain fields
          use custom correctors, use the ``correctors`` argument to pass a
          dictionary mapping field names to :class:`whoosh.spelling.Corrector`
          objects.

        * ONLY CORRECTS FIELDS THAT HAVE THE ``spelling`` ATTRIBUTE in the
          schema (or for which you pass a custom corrector). To automatically
          check all fields, use ``allfields=True``. Spell checking fields
          without ``spelling`` is slower.

        Expert users who want more sophisticated correction behavior can create
        a custom :class:`whoosh.spelling.QueryCorrector` and use that instead
        of this method.

        Returns a :class:`whoosh.spelling.Correction` object with a ``query``
        attribute containing the corrected :class:`whoosh.query.Query` object
        and a ``string`` attributes containing the corrected query string.

        >>> from whoosh import qparser, highlight
        >>> qtext = 'mary "litle lamb"'
        >>> q = qparser.QueryParser("text", myindex.schema)
        >>> mysearcher = myindex.searcher()
        >>> correction = mysearcher().correct_query(q, qtext)
        >>> correction.query
        <query.And ...>
        >>> correction.string
        'mary "little lamb"'
        >>> mysearcher.close()

        You can use the ``Correction`` object's ``format_string`` method to
        format the corrected query string using a
        :class:`whoosh.highlight.Formatter` object. For example, you can format
        the corrected string as HTML, emphasizing the changed words.

        >>> hf = highlight.HtmlFormatter(classname="change")
        >>> correction.format_string(hf)
        'mary "<strong class="change term0">little</strong> lamb"'

        :param q: the :class:`whoosh.query.Query` object to correct.
        :param qstring: the original user query from which the query object was
            created. You can pass None instead of a string, in which the
            second item in the returned tuple will also be None.
        :param correctors: an optional dictionary mapping fieldnames to
            :class:`whoosh.spelling.Corrector` objects. By default, this method
            uses the contents of the index to spell check the terms in the
            query. You can use this argument to "override" some fields with a
            different correct, for example a
            :class:`whoosh.spelling.GraphCorrector`.
        :param terms: a sequence of ``("fieldname", "text")`` tuples to correct
            in the query. By default, this method corrects terms that don't
            appear in the index. You can use this argument to override that
            behavior and explicitly specify the terms that should be corrected.
        :param maxdist: the maximum number of "edits" (insertions, deletions,
            subsitutions, or transpositions of letters) allowed between the
            original word and any suggestion. Values higher than ``2`` may be
            slow.
        :param prefix: suggested replacement words must share this number of
            initial characters with the original word. Increasing this even to
            just ``1`` can dramatically speed up suggestions, and may be
            justifiable since spellling mistakes rarely involve the first
            letter of a word.
        :rtype: :class:`whoosh.spelling.Correction`
        """

        reader = self.reader()

        # Dictionary of custom per-field correctors
        if correctors is None:
            correctors = {}

        # Fill in default corrector objects for fields that don't have a custom
        # one in the "correctors" dictionary
        fieldnames = self.schema.names()
        for fieldname in fieldnames:
            if fieldname not in correctors:
                correctors[fieldname] = self.reader().corrector(fieldname)

        # Get any missing terms in the query in the fields we're correcting
        if terms is None:
            terms = []
            for fieldname, text in q.terms():
                if fieldname in correctors and (
                fieldname, text) not in reader:
                    # Note that we use the original, not aliases fieldname here
                    # so if we correct the query we know what it was
                    terms.append((fieldname, text))

        # Make q query corrector
        from whoosh import spelling
        sqc = spelling.SimpleQueryCorrector(correctors, terms,
                                            prefix=prefix,
                                            maxdist=maxdist)
        return sqc.correct_query(q, qstring)


class MultiSearcher(SearcherType):
    def __init__(self, mreader: 'reading.MultiReader',
                 weighting: 'scoring.WeightingModel'=None,
                 closereader: bool=True,
                 fromindex: 'index.Index'=None):
        super().__init__(weighting)

        self._from_index = fromindex
        self._mreader = mreader
        self._readers = mreader.readers
        self._closereader = closereader
        self._subsearchers = [
            (Searcher(r, weighting=weighting, parent=self), offset)
            for r, offset in self._mreader.leaf_readers()
        ]

    @property
    def schema(self) -> 'Optional[fields.Schema]':
        if self._readers:
            return self._readers[0].schema
        else:
            return None

    @property
    def weighting(self) -> 'scoring.WeightingModel':
        return self._weighting

    def is_atomic(self):
        return False

    def index(self) -> 'index.Index':
        return self._from_index

    def leaf_searchers(self) -> 'Sequence[Tuple[Searcher, int]]':
        return self._subsearchers

    def up_to_date(self):
        ix = self._from_index
        if not ix:
            raise Exception("The searcher has no reference to its index")
        current = ix.latest_generation()
        return all(r.generation() == current for r in self._readers)

    def refresh(self):
        if self.up_to_date():
            return self

        # Get a new reader, re-using resources from the current reader if
        # possible
        self.closed = True
        newreader = self._from_index.reader(reuse=self._mreader)
        return SearcherType.from_reader(newreader, fromindex=self._from_index,
                                        weighting=self.weighting)

    def reader(self) -> 'reading.IndexReader':
        return self._mreader


class Searcher(SearcherType):
    """
    Wraps an :class:`~whoosh.reading.IndexReader` object and provides
    methods for searching the index.
    """

    def __init__(self, reader: 'reading.IndexReader',
                 weighting: 'scoring.WeightingModel'=None,
                 closereader: bool=True,
                 fromindex: 'index.Index'=None,
                 parent: 'SearcherType'=None):
        """
        :param reader: An :class:`~whoosh.reading.IndexReader` object for
            the index to search.
        :param weighting: A :class:`whoosh.scoring.WeightingModel` object to use
            to score documents.
        :param closereader: Whether the underlying reader will be closed when
            the searcher is closed.
        :param fromindex: An optional reference to the index of the underlying
            reader. This is required for :meth:`Searcher.up_to_date` and
            :meth:`Searcher.refresh` to work.
        :param parent: the parent searcher if this is a sub-searcher.
        """

        super().__init__(weighting)

        self._reader = reader
        self._closereader = closereader
        self._from_index = fromindex

        # If this is a sub-searcher, take a weak reference to the parent, and
        # use the parent's schema and IDFs
        if parent:
            self._parent = weakref.ref(parent)
            self._schema = parent.schema
            self.idf_cache = parent.idf_cache
            self.field_caches = parent.field_caches
            self.filter_cache = parent.filter_cache
        else:
            self._parent = None
            self._schema = self.reader().schema

    @property
    def schema(self):
        if self._parent:
            return self._parent().schema
        else:
            return self._schema

    @property
    def weighting(self):
        if self._parent:
            return self._parent().weighting
        else:
            return self._weighting

    def index(self) -> 'index.Index':
        return self._from_index

    def leaf_searchers(self) -> 'Sequence[Tuple[Searcher, int]]':
        return [(self, 0)]

    def reader(self) -> 'reading.IndexReader':
        return self._reader




