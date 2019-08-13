# Copyright 2015 Matt Chaput. All rights reserved.
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

from abc import abstractmethod
from typing import Dict

from whoosh import index, metadata
from whoosh.codec import codecs


# Exceptions

class StorageError(Exception):
    pass


class ReadOnlyError(StorageError):
    pass


class WriteOnlyError(StorageError):
    pass


class TocNotFound(StorageError):
    pass


# URL registry

registry = {}  # type: Dict[str, type]


# Class decorator to add a class to the registry
def url_handler(cls):
    method = getattr(cls, "from_url")
    if not method or not callable(method):
        raise Exception("%r does not have a valid from_url method" % cls)
    if not hasattr(cls, "url_scheme"):
        raise Exception("%r does not have a url_scheme attribute" % cls)
    scheme = cls.url_scheme
    if not isinstance(scheme, str):
        raise TypeError("URL scheme %r is not a string" % scheme)
    if scheme in registry:
        raise NameError("URL scheme %r already registered" % scheme)

    registry[cls.url_scheme] = cls
    return cls


# def get_storage_and_codec(scheme: str) -> Tuple[str, str]:
#     if "+" in scheme:
#         storage_name, codec_name = scheme.rsplit("+", 1)
#     else:
#         storage_name = scheme
#         codec_name = None
#     return storage_name, codec_name
#
#
# def from_url(url: str) -> 'Storage':
#     url = furl.furl(url)
#     scheme = url.scheme
#     storage_name, _ = get_storage_and_codec(scheme)
#     storage_cls = registry[storage_name]
#     assert issubclass(storage_cls, Storage)
#     return storage_cls.from_url(url)


# Base classes

class Lock:
    # This is a typing system stand in for any object that implements the lock
    # protocol

    def acquire(self) -> bool:
        pass

    def release(self):
        pass


class Session:
    def __init__(self, store: 'Storage', indexname: str, writable: bool,
                 id_counter: int):
        self.store = store
        self.indexname = indexname
        self.id_counter = id_counter
        self._writable = writable

    def __repr__(self):
        return "<%s %r %s>" % (
            type(self).__name__, self.indexname, self._writable
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def is_writable(self) -> bool:
        return self._writable

    def next_id(self) -> int:
        counter = self.id_counter
        self.id_counter += 1
        return counter

    def read_key(self) -> int:
        """
        If the corresponding Storage object support recursive locks for writing,
        this returns the key value stored with the lock. Otherwise it raises an
        exception.
        """

        raise Exception("This session does not support recursive locking")

    def close(self):
        pass


class Storage:
    """
    Base class for Storage implementations. A Storage represents the source of
    index data, for example a directory of files or a database.

    Important: Storage objects should be pickle-able, to enable multiprocessing.
    A Storage implementation should avoid having the Storage object hold
    un-pickle-able resources (such as database connections or open files), or
    use ``__getstate__`` and ``__setstate__`` to make pickling possible.
    """

    @classmethod
    def from_url(cls, url: str) -> 'Storage':
        raise Exception("%r does not support construction from URL" % cls)

    def as_url(self) -> str:
        raise Exception("%s does not support construction of URL" % self)

    def supports_multiproc_writing(self) -> bool:
        return False

    def create(self):
        """
        Creates any required implementation-specific resources. For example,
        a filesystem-based implementation might create a directory, while a
        database implementation might create tables. For example::

            from whoosh.filedb.filestore import FileStorage
            # Create a storage object
            st = FileStorage("indexdir")
            # Create any necessary resources
            st.create()

        This method returns ``self`` so you can also say::

            st = FileStorage("indexdir").create()

        Storage implementations should be written so that calling create() a
        second time on the same storage

        :return: a :class:`Storage` instance.
        """

        return self

    def destroy(self, *args, **kwargs):
        """
        Removes any implementation-specific resources related to this storage
        object. For example, a filesystem-based implementation might delete a
        directory, and a database implementation might drop tables.

        The arguments are implementation-specific.
        """

        pass

    def open(self, indexname: str=None, writable: bool=False) -> Session:
        """
        This is a low-level method. You will usually call ``create_index`` or
        ``open_index`` instead.

        Returns an object representing an open transaction with this storage.
        For example, for a database backend the Session object would represent
        and open connection. Other backends, such as the default filesystem
        backend, have no concept of a session and will simply return a dummy
        object.

        :param indexname: the name of the index to open within the storage.
        :param writable: whether this session should allow writing.
        """

        indexname = indexname or index.DEFAULT_INDEX_NAME
        return Session(self, indexname, writable)

    def recursive_write_open(self, key: int, indexname: str=None) -> Session:
        """
        This is only implemented for Storage schemes that can allow
        "sub-writers" in other threads/processes to write to the index while
        it's locked (for example, FileStorage).

        :param key: when the index lock is acquired, it writes a random key
            integer to the lock and remembers it. The code checks that the key
            you pass here matches that key, as a very simple double-check that
            the code isn't writing somewhere it shouldn't.
        :param indexname: the name of the index to open.
        """

        raise Exception("This storage does not allow recursive opening")

    @abstractmethod
    def temp_storage(self, name: str=None) -> 'Storage':
        """
        Creates a new storage object for temporary files. You can call
        :meth:`Storage.destroy` on the new storage when you're finished with
        it.

        :param name: a name for the new storage. This may be optional or
            required depending on the storage implementation.
        :rtype: :class:`BaseFileStorage`
        """

        raise NotImplementedError

    def cleanup(self, session: Session, toc: 'index.Toc'=None):
        """
        Cleans up any old data in the storage.

        :param session: the session object to clean up in.
        :param toc: the TOC object to use for the current data. If this is not
            given, the method loads the current TOC.
        """

        pass

    def clean_segment(self, session: Session, segment: 'codecs.Segment'):
        """
        Cleans up any data in the storage associated with a deleted or merged
        segment.

        :param session: the session object to clean up in.
        :param segment: represents the deleted segment. Storage implementations
            must typically have some coupling to the codec and segment
            implementations to know _how_ to delete segment data from storage.
        """

        pass

    @abstractmethod
    def save_toc(self, session: Session, toc: 'index.Toc'):
        raise NotImplementedError

    @abstractmethod
    def load_toc(self, session: Session, generation: int=None) -> 'index.Toc':
        raise NotImplementedError

    @abstractmethod
    def latest_generation(self, session: Session) -> int:
        raise NotImplementedError

    @abstractmethod
    def lock(self, name: str) -> Lock:
        """
        Return a named lock object (implementing ``.acquire()`` and
        ``.release()`` methods). Different storage implementations may use
        different lock types with different guarantees.

        :param name: a name for the lock.
        :return: a Lock-like object.
        """

        raise NotImplementedError

    # Convenience methods

    def index_exists(self, indexname: str) -> bool:
        with self.open(indexname) as session:
            try:
                # Try loading a TOC with that name to see if we get an error
                _ = self.load_toc(session)
            except metadata.FileHeaderError:
                return False
            except TocNotFound:
                return False

            return True

    def open_index(self, indexname: str=None, generation: int=None,
                   schema=None):
        """
        Opens an existing index (created using :meth:`create_index`) in this
        storage.

        >>> from whoosh.filedb.filestore import FileStorage
        >>> st = FileStorage("indexdir")
        >>> # Open an index in the storage
        >>> ix = st.open_index()

        :param indexname: the name of the index within the storage object. You
            can use this option to store multiple indexes in the same storage.
        :param generation: specify a generation to try to load.
        :param schema: if you pass in a :class:`whoosh.fields.Schema` object
            using this argument, it will override the schema that was stored
            with the index.
        """

        from whoosh import index

        indexname = indexname or index.DEFAULT_INDEX_NAME
        session = self.open(indexname)
        try:
            toc = self.load_toc(session, generation=generation)
        except TocNotFound:
            raise index.EmptyIndexError

        return index.Index(self, indexname, schema)

    def create_index(self, schema, indexname: str=None):
        """
        Creates a new index in this storage.

        >>> from whoosh import fields
        >>> from whoosh.filedb.filestore import FileStorage
        >>> schema = fields.Schema(content=fields.TEXT)
        >>> # Create the storage directory
        >>> st = FileStorage("indexdir")
        >>> st.create()
        >>> # Create an index in the storage
        >>> ix = st.create_index(schema)

        :param schema: the :class:`whoosh.fields.Schema` object to use for the
            new index.
        :param indexname: the name of the index within the storage object. You
            can use this option to store multiple indexes in the same storage.
        :param indexclass: an optional custom ``Index`` sub-class to use to
            create the index files. The default is
            :class:`whoosh.index.FileIndex`. This method will call the
            ``create`` class method on the given class to create the index.
        :return: a :class:`whoosh.index.Index` instance.
        """

        from whoosh import index

        if hasattr(self, "readonly") and self.readonly:
            raise ReadOnlyError
        indexname = indexname or index.DEFAULT_INDEX_NAME

        # Create an empty initial TOC
        toc = index.Toc(schema, [], 0)

        # Write the TOC to disk
        with self.open(indexname, writable=True) as session:
            self.cleanup(session, toc)
            self.save_toc(session, toc)

        # Return an Index with the new TOC
        return index.Index(self, indexname, schema)

    def copy_index(self, to_storage: "Storage", indexname: str):
        raise NotImplementedError
