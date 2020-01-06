# Copyright 2017 Matt Chaput. All rights reserved.
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

import struct
from array import array
from bisect import bisect_left
from typing import Callable, Iterable, List, Sequence, Tuple

from whoosh.postings import postform, postings, ptuples
from whoosh.postings.postings import RawPost, PostTuple
from whoosh.postings.ptuples import (DOCID, TERMBYTES, LENGTH, WEIGHT,
                                     POSITIONS, RANGES, PAYLOADS)
from whoosh.system import IS_LITTLE
from whoosh.util.numlists import delta_encode, delta_decode, min_array_code


# Enum specifying how weights are stored in a posting block
NO_WEIGHTS = 0  # the block has no weights
ALL_ONES = 1  # all the weights were 1.0, so we stored this flag instead of them
ALL_INTS = 2  # all the weights were whole, so they're stored as ints not floats
FLOAT_WEIGHTS = 4  # weights are stored as floats

# Struct for encoding the length typecode and count of a list of byte chunks
tcodes_and_len = struct.Struct("<ccI")

# Flags bitfield
HAS_LENGTHS = 1 << 0
HAS_WEIGHTS = 1 << 1
HAS_POSITIONS = 1 << 2
HAS_RANGES = 1 << 3
HAS_PAYLOADS = 1 << 4

# When the "flags" byte is this value, it means the post block has only one
# piece of information -- a single Doc ID. We use a special minimal byte
# representation and reader for this case.
MIN_POSTS_FLAG = 0b10000000
MIN_TYPE_MASK = 0b01100000
MIN_LENGTH_MASK = 0b00011111
MIN_TYPE_CODES = ("B", "H", "I", "q")


def make_flags(has_lengths=False, has_weights=False, has_positions=False,
               has_ranges=False, has_payloads=False) -> int:
    return (
        (HAS_LENGTHS if has_lengths else 0) |
        (HAS_WEIGHTS if has_weights else 0) |
        (HAS_POSITIONS if has_positions else 0) |
        (HAS_RANGES if has_ranges else 0) |
        (HAS_PAYLOADS if has_payloads else 0)
    )


# Helper functions

def min_array(nums: Sequence[int]) -> array:
    """
    Takes a sequence of integers and returns an array using the smallest
    typecode necessary to represent the numbers in the sequence.
    """

    code = min_array_code(max(nums))
    return array(code, nums)


# Basic implementations of on-disk posting format

class BasicIO(postings.PostingsIO):
    # B   - Flags (has_*)
    # H   - Number of postings in block
    # 2c  - IDs and weights typecodes
    # ii  - Min/max length
    # iii - positions, ranges, payloads data lengths
    doc_header = struct.Struct("<BH2ciiiii")

    # B   - Flags (has_*)
    # I   - Document number
    min_byte = struct.Struct("B")
    USE_MIN = True

    # B   - Flags (has_*)
    # H   - Number of terms in vector
    # 2c  - IDs and weights typecodes
    # iii - positions, ranges, payloads data lengths
    vector_header = struct.Struct("<Bi2ciii")

    @classmethod
    def pack_doc_header(cls, flags: int, count: int, minlen: int, maxlen: int,
                        ids_typecode: str, weights_typecode: str,
                        poslen: int, rangeslen: int, paylen: int
                        ) -> bytes:
        return cls.doc_header.pack(
            flags, count,
            ids_typecode.encode("ascii"), weights_typecode.encode("ascii"),
            minlen, maxlen,
            poslen, rangeslen, paylen
        )

    @classmethod
    def unpack_doc_header(cls, src: bytes, offset: int) -> Tuple:
        h = cls.doc_header
        flags, count, idc, wc, minlen, maxlen, poslen, rangeslen, paylen = \
            h.unpack(src[offset:offset + h.size])

        ids_typecode = str(idc.decode("ascii"))
        weights_typecode = str(wc.decode("ascii"))

        return (flags, count, ids_typecode, weights_typecode, minlen, maxlen,
                poslen, rangeslen, paylen, offset + h.size)

    @classmethod
    def pack_vector_header(cls, flags: int, count: int,
                           terms_typecode: str, weights_typecode: str,
                           poslen: int, rangeslen: int, paylen: int
                           ) -> bytes:
        return cls.vector_header.pack(
            flags, count,
            terms_typecode.encode("ascii"), weights_typecode.encode("ascii"),
            poslen, rangeslen, paylen
        )

    @classmethod
    def unpack_vector_header(cls, src: bytes, offset: int) -> Tuple:
        h = cls.vector_header
        flags, count, idc, wc, poslen, rangeslen, paylen = \
            h.unpack(src[offset:offset + h.size])

        ids_typecode = str(idc.decode("ascii"))
        weights_typecode = str(wc.decode("ascii"))

        return (flags, count, ids_typecode, weights_typecode,
                poslen, rangeslen, paylen,
                offset + h.size)

    def can_copy_raw_to(self, from_fmt: 'postform.Format',
                        to_io: postings.PostingsIO,
                        to_fmt: 'postform.Format') -> bool:
        return (
            type(to_io) is type(self) and
            from_fmt.can_copy_raw_to(to_fmt)
        )

    def doclist_reader(self, src: bytes, offset: int=0
                       ) -> 'postings.DocListReader':
        if offset >= len(src):
            raise IndexError("Offset %d out of range (%d)" %
                             (offset, len(src)))
        if self.USE_MIN:
            flag_byte = self.min_byte.unpack(src[offset:offset + 1])[0]
            # If the high bit is set, this block was minimal encoded
            if flag_byte & MIN_POSTS_FLAG:
                # Pull a 2-bit number representing the array type from the flags
                typenum = (flag_byte & MIN_TYPE_MASK) >> 5
                # Convert it to a typecode string
                typecode = MIN_TYPE_CODES[typenum]
                # Pull the block length from the flags
                length = (flag_byte & MIN_LENGTH_MASK) + 1
                # Create a struct to unpoack the docids
                s = struct.Struct("<" + (typecode * length))
                docids = s.unpack(src[offset + 1:offset + s.size + 1])
                # Return a minimal reader that only knows about doc IDs
                return postings.MinimalDocListReader(docids)

        return BasicDocListReader(src, offset)

    def vector_reader(self, src: bytes, offset: int=0) -> 'BasicVectorReader':
        return BasicVectorReader(src, offset)

    @staticmethod
    def _minimal_doclist_bytes(posts: Sequence[RawPost]) -> bytes:
        assert posts
        # Pull the IDs from the postings
        docids = [p[DOCID] for p in posts]
        # Figure out the smallest array typecode we can use
        typecode = min_array_code(max(docids))
        # Convert that into a 2-bit number
        typenum = MIN_TYPE_CODES.index(typecode)
        length = len(docids)
        # Enocde the "minimal" marker, the typecode, and the block length
        # in a single byte
        flags = MIN_POSTS_FLAG | (typenum << 5) | (length - 1)
        # Enocde the whole thing as bytes using
        s = struct.Struct("<B" + (typecode * length))
        return s.pack(flags, *docids)

    def doclist_to_bytes(self, fmt: postform.Format,
                         posts: Sequence[RawPost]) -> bytes:
        if not posts:
            raise ValueError("Empty document postings list")

        # If the format has no features and the block only has a few posts,
        # we can special-case a minimal format that only stores the doc IDs
        if self.USE_MIN and fmt.only_docids() and 0 < len(posts) <= 32:
            return self._minimal_doclist_bytes(posts)

        # Otherwise, build up the bytes using a flags byte, the header,
        # and the encoded information
        flags = make_flags(fmt.has_lengths, fmt.has_weights, fmt.has_positions,
                           fmt.has_ranges, fmt.has_payloads)
        ids_code, ids_bytes = self.encode_docids([p[DOCID] for p in posts])
        minlen, maxlen, len_bytes = self.extract_lengths(fmt, posts)
        weights_code, weight_bytes = self.extract_weights(fmt, posts)
        pos_bytes, range_bytes, pay_bytes = self.extract_features(fmt, posts)

        header = self.pack_doc_header(
            flags, len(posts), minlen, maxlen, ids_code, weights_code,
            len(pos_bytes), len(range_bytes), len(pay_bytes)
        )
        return b''.join((header, ids_bytes, len_bytes, weight_bytes,
                         pos_bytes, range_bytes, pay_bytes))

    def vector_to_bytes(self, fmt: postform.Format,
                        posts: List[ptuples.PostTuple]) -> bytes:
        if not posts:
            raise ValueError("Empty vector postings list")

        posts = [self.condition_post(p) for p in posts]
        flags = make_flags(fmt.has_lengths, fmt.has_weights, fmt.has_positions,
                           fmt.has_ranges, fmt.has_payloads)
        t_code, t_bytes = self.encode_terms(self._extract(posts, TERMBYTES))
        weights_code, weight_bytes = self.extract_weights(fmt, posts)
        pos_bytes, range_bytes, pay_bytes = self.extract_features(fmt, posts)

        header = self.pack_vector_header(
            flags, len(posts), t_code, weights_code,
            len(pos_bytes), len(range_bytes), len(pay_bytes)
        )
        return b''.join((header, t_bytes, weight_bytes,
                         pos_bytes, range_bytes, pay_bytes))

    def extract_lengths(self, fmt: postform.Format,
                        posts: Sequence[RawPost]
                        ) -> Tuple[int, int, bytes]:
        len_bytes = b''
        minlen = maxlen = 1
        if fmt.has_lengths or fmt.has_weights:
            # Even if the format doesn't store lengths, we still need to compute
            # the maximum and minimum lengths for scoring
            lengths = self._extract(posts, LENGTH)
            minlen = min(lengths)
            maxlen = max(lengths)

            if fmt.has_lengths:
                len_bytes = self.encode_lengths(lengths)

        return minlen, maxlen, len_bytes

    def extract_weights(self, fmt: postform.Format, posts: Sequence[RawPost]
                        ) -> Tuple[str, bytes]:
        if fmt.has_weights:
            weights = self._extract(posts, WEIGHT)
            return self.encode_weights(weights)
        return "0", b''

    def extract_features(self, fmt: postform.Format, posts: Sequence[RawPost]):
        pos_bytes = b''
        if fmt.has_positions:
            poslists = self._extract(posts, POSITIONS)  # type: Sequence[bytes]
            pos_bytes = self.encode_chunk_list(poslists)

        range_bytes = b''
        if fmt.has_ranges:
            rnglists = self._extract(posts, RANGES)  # type: Sequence[bytes]
            range_bytes = self.encode_chunk_list(rnglists)

        pay_bytes = b''
        if fmt.has_payloads:
            paylists = self._extract(posts, PAYLOADS)  # type: Sequence[bytes]
            pay_bytes = self.encode_chunk_list(paylists)

        return pos_bytes, range_bytes, pay_bytes

    # Encoding methods

    def condition_post(self, post: PostTuple) -> RawPost:
        poses = post[POSITIONS]
        enc_poses = self.encode_positions(poses) if poses else None
        ranges = post[RANGES]
        enc_ranges = self.encode_ranges(ranges) if ranges else None
        pays = post[PAYLOADS]
        enc_pays = self.encode_payloads(pays) if pays else None

        return (
            post[DOCID],
            post[TERMBYTES],
            post[LENGTH],
            post[WEIGHT],
            enc_poses,
            enc_ranges,
            enc_pays,
        )

    @staticmethod
    def encode_docids(docids: Sequence[int]) -> Tuple[str, bytes]:
        if not docids:
            raise ValueError
        if any(n < 0 for n in docids):
            raise ValueError("Negative docid in %s" % docids)

        prev = -1
        for docid in docids:
            if docid <= prev:
                raise ValueError("Doc ID %r is negative/out of order" % docid)
            prev = docid

        docarray = min_array(list(delta_encode(docids)))
        # typecode = "I" if docids[-1] <= 4294967296 else "Q"
        # docarray = array(typecode, docids)
        if not IS_LITTLE:
            docarray.byteswap()
        return docarray.typecode, docarray.tobytes()

    @staticmethod
    def decode_docids(src: bytes, offset: int, typecode: str,
                      count: int) -> Tuple[int, Sequence[int]]:
        docarray = array(typecode)
        end = offset + docarray.itemsize * count
        docarray.frombytes(src[offset: end])
        if not IS_LITTLE:
            docarray.byteswap()
        docids = tuple(delta_decode(docarray))
        return end, docids

    @staticmethod
    def encode_terms(terms: Sequence[bytes]) -> Tuple[str, bytes]:
        lens = min_array([len(t) for t in terms])
        if not IS_LITTLE:
            lens.byteswap()
        return lens.typecode, lens.tobytes() + b''.join(terms)

    @staticmethod
    def decode_terms(src: bytes, offset: int, typecode: str, count: int
                     ) -> Tuple[int, Sequence[bytes]]:
        lens = array(typecode)
        lens_size = lens.itemsize * count
        lens.frombytes(src[offset: offset + lens_size])
        offset += lens_size

        terms = []
        for length in lens:
            terms.append(src[offset:offset + length])
            offset += length
        return offset, terms

    @staticmethod
    def encode_lengths(lengths: Sequence[int]) -> bytes:
        if any(not isinstance(n, int) or n < 0 or n > 255 for n in lengths):
            raise ValueError("Bad byte in %r" % lengths)
        arry = array("B", lengths)
        return arry.tobytes()

    @staticmethod
    def decode_lengths(src: bytes, offset: int, count: int) -> Sequence[int]:
        end = offset + count
        len_array = array("B")
        len_array.frombytes(src[offset:end])
        return len_array

    @staticmethod
    def encode_weights(weights: Sequence[float]) -> Tuple[str, bytes]:
        if not weights or any(not isinstance(w, (int, float)) for w in weights):
            raise ValueError("Bad weight in %r" % weights)

        if all(w == 1 for w in weights):
            return "1", b""

        intweights = [int(w) for w in weights]
        if all(w == wi for w, wi in zip(weights, intweights)):
            arr = min_array(intweights)
        else:
            arr = array("f", weights)
        if not IS_LITTLE:
            arr.byteswap()

        return arr.typecode, arr.tobytes()

    @staticmethod
    def decode_weights(src: bytes, offset: int, typecode: str, count: int
                       ) -> Sequence[float]:
        if typecode == "0":
            raise Exception("Weights were not encoded")
        elif typecode == "1":
            return array("f", (1.0 for _ in range(count)))

        weights = array(typecode)
        weights.frombytes(src[offset: offset + weights.itemsize * count])
        if not IS_LITTLE:
            weights.byteswap()
        return weights

    @staticmethod
    def compute_weights_size(typecode: str) -> int:
        if typecode == "0":
            return 0
        if typecode == "1":
            return 0
        else:
            return struct.calcsize(typecode)

    @staticmethod
    def encode_positions(poses: Sequence[int]) -> bytes:
        deltas = min_array(list(delta_encode(poses)))
        if not IS_LITTLE:
            deltas.byteswap()
        return deltas.typecode.encode("ascii") + deltas.tobytes()

    @staticmethod
    def decode_positions(src: bytes, offset: int, size: int) -> Sequence[int]:
        if size == 0:
            return ()

        typecode = str(bytes(src[offset:offset + 1]).decode("ascii"))
        deltas = array(typecode)
        deltas.frombytes(src[offset + 1:offset + size])
        if not IS_LITTLE:
            deltas.byteswap()
        return tuple(delta_decode(deltas))

    @staticmethod
    def encode_ranges(ranges: Sequence[Tuple[int, int]]) -> bytes:
        base = 0
        deltas = []
        for start, end in ranges:
            if start < base:
                raise ValueError("range indices out of order: %s %s"
                                 % (base, start))
            if end < start:
                raise ValueError("Negative range: %s %s" % (start, end))

            deltas.append(start - base)
            deltas.append(end - start)
            base = end
        deltas = min_array(deltas)
        return deltas.typecode.encode("ascii") + deltas.tobytes()

    @staticmethod
    def decode_ranges(src: bytes, offset: int, size: int
                      ) -> Sequence[Tuple[int, int]]:
        if size == 0:
            return ()

        typecode = str(bytes(src[offset:offset + 1]).decode("ascii"))
        indices = array(typecode)
        indices.frombytes(src[offset + 1:offset + size])
        if IS_LITTLE:
            indices.byteswap()

        if len(indices) % 2:
            raise Exception("Odd number of range indices: %r" % indices)

        # Zip up the linear list into pairs, and at the same time delta-decode
        # the numbers
        base = 0
        cs = []
        for i in range(0, len(indices), 2):
            start = base + indices[i]
            end = start + indices[i + 1]
            cs.append((start, end))
            base = end
        return cs

    @staticmethod
    def encode_payloads(payloads: Sequence[bytes]) -> bytes:
        return BasicIO.encode_chunk_list(payloads)

    @staticmethod
    def decode_payloads(src: bytes, offset: int, size: int) -> Sequence[bytes]:
        if size == 0:
            return ()

        return BasicIO.decode_chunk_list(src, offset, size)

    @staticmethod
    def encode_chunk_list(chunks: Sequence[bytes]) -> bytes:
        # Encode the lengths of the chunks
        lens = [len(chunk) for chunk in chunks]
        len_array = min_array(lens)
        if not IS_LITTLE:
            len_array.byteswap()

        # Encode the offsets from the lengths (unfortunately rebuilding this
        # information from the lengths is SLOW, so we have to encode it)
        base = 0
        offsets = []
        for length in len_array:
            offsets.append(base)
            base += length
        offsets_array = min_array(offsets)

        # Encode the header
        header = tcodes_and_len.pack(offsets_array.typecode.encode("ascii"),
                                     len_array.typecode.encode("ascii"),
                                     len(chunks))
        index = [header, offsets_array.tobytes(), len_array.tobytes()]
        return b"".join(index + chunks)

    @staticmethod
    def decode_chunk_index(src: bytes, offset: int
                           ) -> Sequence[Tuple[int, int]]:
        # Decode the header
        h_end = offset + tcodes_and_len.size
        off_code, lens_code, count = tcodes_and_len.unpack(src[offset:h_end])
        off_code = str(off_code.decode("ascii"))
        lens_code = str(lens_code.decode("ascii"))

        # Load the offsets array
        off_array = array(off_code)
        off_end = h_end + off_array.itemsize * count
        off_array.frombytes(src[h_end: off_end])
        if not IS_LITTLE:
            off_array.byteswap()

        # Load the lengths array
        len_array = array(lens_code)
        lens_end = off_end + len_array.itemsize * count
        len_array.frombytes(src[off_end: lens_end])
        if not IS_LITTLE:
            len_array.byteswap()

        # Translate the local offsets to global offsets
        offsets = [lens_end + off for off in off_array]
        return list(zip(offsets, len_array))

    @staticmethod
    def decode_chunk_list(src: bytes, offset: int, size: int
                          ) -> Sequence[bytes]:
        ix = BasicIO.decode_chunk_index(src, offset)
        return tuple(bytes(src[chunk_off:chunk_off + length])
                     for chunk_off, length in ix)


# Reading classes

class BasicPostingReader(postings.PostingReader):
    # Common superclass for Doclist and Vector readers
    def __init__(self, source: bytes, offset: int):
        self._src = source
        self._offset = offset

        # Dummy slots so the IDE won't complain about methods on this class
        # accessing them
        self._count = None  # type: int
        self._end_offset = None  # type: int

        self._lens_offset = None  # type: int
        self._weights_tc = None  # type: str
        self._weights_offset = None  # type: int
        self._weights_size = None  # type: int

        self._poses_offset = None  # type: int
        self._poses_size = None  # type: int
        self._ranges_offset = None  # type: int
        self._ranges_size = None  # type: int
        self._pays_offset = None  # type: int
        self._pays_size = None  # type: int

        # Slots for demand-loaded data
        self._weights_type = NO_WEIGHTS
        self._weights = None
        self._chunk_indexes = [None, None, None]

    def _setup_offsets(self, offset: int):
        wtc = self._weights_tc
        if wtc == "0":
            self._weights_type = NO_WEIGHTS
        elif wtc == "1":
            self._weights_type = ALL_ONES
        elif wtc == "f":
            self._weights_type = FLOAT_WEIGHTS
        else:
            self._weights_type = ALL_INTS

        # Set up the weights offsets
        self._weights_offset = offset
        wts_itemsize = BasicIO.compute_weights_size(wtc)

        self._weights_size = wts_itemsize * self._count

        # Compute the offset of feature sections based on their sizes
        self._poses_offset = offset + self._weights_size
        self._ranges_offset = self._poses_offset + self._poses_size
        self._pays_offset = self._ranges_offset + self._ranges_size
        self._end_offset = self._pays_offset + self._pays_size

    def raw_bytes(self) -> bytes:
        return self._src[self._offset: self._end_offset]

    def size_in_bytes(self) -> int:
        return self._end_offset - self._offset

    def _get_weights(self) -> Sequence[float]:
        if self._weights is None:
            self._weights = BasicIO.decode_weights(
                self._src, self._weights_offset, self._weights_tc, self._count
            )
        return self._weights

    def weight(self, n: int) -> float:
        if n < 0 or n >= self._count:
            raise IndexError

        wt = self._weights_type
        if wt == NO_WEIGHTS or wt == ALL_ONES:
            return 1.0
        else:
            return self._get_weights()[n]

    def total_weight(self) -> float:
        wt = self._weights_type
        if wt == NO_WEIGHTS or wt == ALL_ONES:
            return self._count
        else:
            return sum(self._get_weights())

    def max_weight(self):
        wt = self._weights_type
        if wt == NO_WEIGHTS or wt == ALL_ONES:
            return 1.0
        else:
            return max(self._get_weights())

    def _chunk_offsets(self, n: int, offset: int, size: int,
                       ix_pos: int) -> Tuple[int, int]:
        assert size

        if n < 0 or n >= self._count:
            raise IndexError
        if not size:
            raise postings.UnsupportedFeature

        ix = self._chunk_indexes[ix_pos]
        if ix is None:
            ix = BasicIO.decode_chunk_index(self._src, offset)
            self._chunk_indexes[ix_pos] = ix

        return ix[n]

    def positions(self, n: int) -> Sequence[int]:
        if not self._poses_size:
            return ()

        offset, length = self._chunk_offsets(n, self._poses_offset,
                                             self._poses_size, 0)
        return BasicIO.decode_positions(self._src, offset, length)

    def raw_positions(self, n: int) -> bytes:
        if not self._poses_size:
            return b''

        offset, length = self._chunk_offsets(n, self._poses_offset,
                                             self._poses_size, 0)
        return self._src[offset: offset + length]

    def ranges(self, n: int) -> Sequence[Tuple[int, int]]:
        if not self._ranges_size:
            return ()

        offset, length = self._chunk_offsets(n, self._ranges_offset,
                                             self._ranges_size, 1)
        return BasicIO.decode_ranges(self._src, offset, length)

    def raw_ranges(self, n: int) -> bytes:
        if not self._ranges_size:
            return b''

        offset, length = self._chunk_offsets(n, self._ranges_offset,
                                             self._ranges_size, 1)
        return self._src[offset: offset + length]

    def payloads(self, n: int) -> Sequence[bytes]:
        if not self._pays_size:
            return ()

        offset, length = self._chunk_offsets(n, self._pays_offset,
                                             self._pays_size, 2)
        return BasicIO.decode_payloads(self._src, offset, length)

    def raw_payloads(self, n: int) -> Sequence[bytes]:
        if not self._pays_size:
            return b''

        offset, length = self._chunk_offsets(n, self._pays_offset,
                                             self._pays_size, 2)
        return self._src[offset: offset + length]


class BasicDocListReader(BasicPostingReader, postings.DocListReader):
    def __init__(self, src: bytes, offset: int=0):
        super(BasicDocListReader, self).__init__(src, offset)
        self._lens = None

        # Unpack the header
        (flags, self._count, self._ids_tc, self._weights_tc, self._min_len,
         self._max_len, self._poses_size, self._ranges_size, self._pays_size,
         self._h_end) = BasicIO.unpack_doc_header(src, offset)

        # Copy feature flags from flags
        self.has_lengths = bool(flags & HAS_LENGTHS)
        self.has_weights = bool(flags & HAS_WEIGHTS)
        self.has_positions = bool(flags & HAS_POSITIONS)
        self.has_ranges = bool(flags & HAS_RANGES)
        self.has_payloads = bool(flags & HAS_PAYLOADS)

        # Read the IDs
        offset, self._ids = BasicIO.decode_docids(src, self._h_end,
                                                  self._ids_tc, self._count)

        # Set up lengths if the format stores them
        if self.has_lengths:
            self._lens_offset = offset
            offset += self._count

        # Set up offsets/sizes for other features (also self._end_offset)
        self._setup_offsets(offset)

    def __len__(self):
        return self._count

    def __repr__(self):
        return "<%s %d>" % (type(self).__name__, self._count)

    def id(self, n: int) -> int:
        if n < 0 or n >= self._count:
            raise IndexError("%r/%s" % (n, self._count))

        return self._ids[n]

    def id_slice(self, start: int, end: int) -> Sequence[int]:
        return self._ids[start:end]

    def all_ids(self) -> Sequence[int]:
        return self._ids

    def rewrite_raw_bytes(self, docmap_get: Callable[[int, int], int]
                          ) -> bytes:
        offset = self._h_end
        rawbytes = bytearray(self.raw_bytes())
        docids = self.all_ids()
        if docmap_get:
            docids = [docmap_get(docid, docid) for docid in docids]
        newtc, newbytes = BasicIO.encode_docids(docids)
        if newtc != self._ids_tc:
            raise ValueError
        rawbytes[offset:offset + len(newbytes)] = newbytes
        return bytes(rawbytes)

    def _get_lens(self) -> Sequence[int]:
        if self._lens is None:
            if self._lens_offset is None:
                raise postings.UnsupportedFeature
            self._lens = BasicIO.decode_lengths(self._src, self._lens_offset,
                                                self._count)
        return self._lens

    def length(self, n: int):
        if n < 0 or n >= self._count:
            raise IndexError
        if not self._count:
            raise postings.UnsupportedFeature

        return self._get_lens()[n]

    def min_length(self):
        return self._min_len

    def max_length(self):
        return self._max_len

    def raw_posting_at(self, n: int) -> ptuples.RawPost:
        docid = self.id(n)
        length = self.length(n) if self.has_lengths else None
        weight = self.weight(n) if self.has_weights else None

        posbytes = charbytes = paybytes = None
        if self.has_positions:
            posbytes = self.raw_positions(n)
        if self.has_ranges:
            charbytes = self.raw_ranges(n)
        if self.has_payloads:
            paybytes = self.raw_payloads(n)

        return docid, None, length, weight, posbytes, charbytes, paybytes

    def can_copy_raw_to(self, to_io: 'postings.PostingsIO',
                        to_fmt: 'postform.Format') -> bool:
        fmt = postform.Format(self.has_lengths, self.has_weights,
                              self.has_positions, self.has_ranges,
                              self.has_payloads)
        return BasicIO().can_copy_raw_to(fmt, to_io, to_fmt)


class BasicVectorReader(BasicPostingReader, postings.VectorReader):
    def __init__(self, src: bytes, offset: int=0):
        super(BasicVectorReader, self).__init__(src, offset)

        # Unpack the header
        (flags, self._count, t_typecode, self._weights_tc,
         self._poses_size, self._ranges_size, self._pays_size,
         h_end) = BasicIO.unpack_vector_header(src, offset)

        # Copy feature flags from flags
        self.has_lengths = bool(flags & HAS_LENGTHS)
        self.has_weights = bool(flags & HAS_WEIGHTS)
        self.has_positions = bool(flags & HAS_POSITIONS)
        self.has_ranges = bool(flags & HAS_RANGES)
        self.has_payloads = bool(flags & HAS_PAYLOADS)

        # Read the terms
        offset, self._terms = BasicIO.decode_terms(src, h_end, t_typecode,
                                                   self._count)

        # Set up offsets/sizes for other features (also self._end_offset)
        self._setup_offsets(offset)

    def __len__(self):
        return self._count

    def all_terms(self) -> Iterable[bytes]:
        for tbytes in self._terms:
            yield tbytes

    def termbytes(self, n: int) -> bytes:
        if n < 0 or n >= self._count:
            raise IndexError

        return self._terms[n]

    def seek(self, termbytes: bytes) -> int:
        return bisect_left(self._terms, termbytes)

    def term_index(self, termbytes: bytes) -> int:
        i = self.seek(termbytes)
        if i < len(self) and self._terms[i] == termbytes:
            return i
        else:
            raise KeyError(termbytes)

    def can_copy_raw_to(self, to_io: 'postings.PostingsIO',
                        to_fmt: 'postform.Format') -> bool:
        from_fmt = postform.Format(self.has_lengths, self.has_weights,
                                   self.has_positions, self.has_ranges,
                                   self.has_payloads)
        return BasicIO().can_copy_raw_to(from_fmt, to_io, to_fmt)


