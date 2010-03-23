
#===============================================================================
# Copyright 2010 Matt Chaput
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#===============================================================================

import os, shutil, tempfile, time
from array import array
from collections import defaultdict
from heapq import heapify, heappush, heappop
from marshal import load, dump
from multiprocessing import Process, Queue
from struct import Struct

from whoosh.filedb.filetables import LengthWriter, LengthReader, MemoryLengthReader
from whoosh.filedb.structfile import StructFile
from whoosh.util import length_to_byte, now


_2int_struct = Struct("!II")
pack2ints = _2int_struct.pack
unpack2ints = _2int_struct.unpack

_length_struct = Struct("!IHB") # Docnum, fieldnum, lengthbyte
pack_length = _length_struct.pack
unpack_length = _length_struct.unpack


#def encode_posting(fieldNum, text, doc, freq, datastring):
#    """Encodes a posting as a string, for sorting.
#    """
#
#    return "".join((pack_ushort(fieldNum),
#                    utf8encode(text)[0],
#                    chr(0),
#                    pack2ints(doc, freq),
#                    datastring))
#
#def decode_posting(posting):
#    """Decodes an encoded posting string into a
#    (field_number, text, document_number, datastring) tuple.
#    """
#
#    field_num = unpack_ushort(posting[:_SHORT_SIZE])[0]
#
#    zero = posting.find(chr(0), _SHORT_SIZE)
#    text = utf8decode(posting[_SHORT_SIZE:zero])[0]
#
#    metastart = zero + 1
#    metaend = metastart + _INT_SIZE * 2
#    doc, freq = unpack2ints(posting[metastart:metaend])
#
#    datastring = posting[metaend:]
#
#    return field_num, text, doc, freq, datastring


def imerge(iterators):
    current = []
    for g in iterators:
        try:
            current.append((g.next(), g))
        except StopIteration:
            pass
    heapify(current)
    
    while len(current) > 1:
        item, gen = heappop(current)
        yield item
        try:
            heappush(current, (gen.next(), gen))
        except StopIteration:
            pass
    
    if current:
        item, gen = current[0]
        yield item
        for item in gen:
            yield item

def bimerge(iter1, iter2):
    try:
        p1 = iter1.next()
    except StopIteration:
        for p2 in iter2:
            yield p2
        return
    
    try:
        p2 = iter2.next()
    except StopIteration:
        for p1 in iter1:
            yield p1
        return
            
    while True:
        if p1 < p2:
            yield p1
            try:
                p1 = iter1.next()
            except StopIteration:
                for p2 in iter2:
                    yield p2
                return
        else:
            yield p2
            try:
                p2 = iter2.next()
            except StopIteration:
                for p1 in iter1:
                    yield p1
                return

def dividemerge(iters):
    length = len(iters)
    if length == 0:
        return []
    if length == 1:
        return iters[0]
    
    mid = length >> 1
    return bimerge(dividemerge(iters[:mid]), dividemerge(iters[mid:]))
    

def read_run(filename, count):
    f = open(filename, "rb")
    while count:
        count -= 1
        yield load(f)
    f.close()


def write_postings(schema, termtable, lengths, postwriter, postiter):
    # This method pulls postings out of the posting pool (built up as
    # documents are added) and writes them to the posting file. Each time
    # it encounters a posting for a new term, it writes the previous term
    # to the term index (by waiting to write the term entry, we can easily
    # count the document frequency and sum the terms by looking at the
    # postings).

    current_fieldnum = None # Field number of the current term
    current_text = None # Text of the current term
    first = True
    current_freq = 0
    offset = None
    getlength = lengths.get

    # Loop through the postings in the pool. Postings always come out of
    # the pool in (field number, lexical) order.
    for fieldnum, text, docnum, freq, valuestring in postiter:
        # Is this the first time through, or is this a new term?
        if first or fieldnum > current_fieldnum or text > current_text:
            if first:
                first = False
            else:
                # This is a new term, so finish the postings and add the
                # term to the term table
                postcount = postwriter.finish()
                termtable.add((current_fieldnum, current_text),
                              (current_freq, offset, postcount))

            # Reset the post writer and the term variables
            current_fieldnum = fieldnum
            current_text = text
            current_freq = 0
            offset = postwriter.start(fieldnum)

        elif (fieldnum < current_fieldnum
              or (fieldnum == current_fieldnum and text < current_text)):
            # This should never happen!
            raise Exception("Postings are out of order: %s:%s .. %s:%s" %
                            (current_fieldnum, current_text, fieldnum, text))

        # Write a posting for this occurrence of the current term
        current_freq += freq
        postwriter.write(docnum, valuestring, getlength(docnum, fieldnum))

    # If there are still "uncommitted" postings at the end, finish them off
    if not first:
        postcount = postwriter.finish()
        termtable.add((current_fieldnum, current_text),
                      (current_freq, offset, postcount))


#class LengthSpool(object):
#    def __init__(self, filename):
#        self.filename = filename
#        self.file = None
#        
#    def create(self):
#        self.file = open(self.filename, "wb")
#        
#    def add(self, docnum, fieldnum, length):
#        self.file.write(pack_length(docnum, fieldnum, length_to_byte(length)))
#        
#    def finish(self):
#        self.file.close()
#        self.file = None
#        
#    def readback(self):
#        f = open(self.filename, "rb")
#        size = _length_struct.size
#        while True:
#            data = f.read(size)
#            if not data: break
#            yield unpack_length(data)
#        f.close()


class PoolBase(object):
    def __init__(self, schema, dir):
        self.schema = schema
        self._dir = dir
        self._fieldlength_totals = defaultdict(int)
        self._fieldlength_maxes = {}
    
    def _filename(self, name):
        return os.path.join(self._dir, name)
    
    def cancel(self):
        pass
    
    def fieldlength_totals(self):
        return dict(self._fieldlength_totals)
    
    def fieldlength_maxes(self):
        return self._fieldlength_maxes
    

class TempfilePool(PoolBase):
    def __init__(self, schema, limitmb=32, dir=None, basename='', **kw):
        if dir is None:
            dir = tempfile.mkdtemp("whoosh")
        PoolBase.__init__(self, schema, dir)
        
        self.length_arrays = {}
        
        self.limit = limitmb * 1024 * 1024
        
        self.size = 0
        self.count = 0
        self.postings = []
        self.runs = []
        
        self.basename = basename
        
        #self.lenspool = LengthSpool(self._filename(basename + "length"))
        #self.lenspool.create()
    
    def add_content(self, docnum, fieldnum, field, value):
        add_posting = self.add_posting
        termcount = 0
        # TODO: Method for adding progressive field values, ie
        # setting start_pos/start_char?
        for w, freq, valuestring in field.index(value):
            #assert w != ""
            add_posting(fieldnum, w, docnum, freq, valuestring)
            termcount += freq
        
        if field.scorable and termcount:
            self.add_field_length(docnum, fieldnum, termcount)
            
        return termcount
    
    def add_posting(self, fieldnum, text, docnum, freq, datastring):
        if self.size >= self.limit:
            #print "Flushing..."
            self.dump_run()

        self.size += len(text) + 2 + 8 + len(datastring)
        self.postings.append((fieldnum, text, docnum, freq, datastring))
        self.count += 1
    
    def add_field_length(self, docnum, fieldnum, length):
        self._fieldlength_totals[fieldnum] += length
        if length > self._fieldlength_maxes.get(fieldnum, 0):
            self._fieldlength_maxes[fieldnum] = length
        #self.lenspool.add(docnum, fieldnum, length)
        
        if fieldnum not in self.length_arrays:
            self.length_arrays[fieldnum] = array("B")
        arry = self.length_arrays[fieldnum]
        if len(arry) <= docnum:
            for _ in xrange(docnum - len(arry) + 1):
                arry.append(0)
        arry[docnum] = length_to_byte(length)
    
    def dump_run(self):
        if self.size > 0:
            tempname = self._filename(self.basename + str(now()) + ".run")
            runfile = open(tempname, "w+b")
            self.postings.sort()
            for p in self.postings:
                dump(p, runfile)
            runfile.close()

            self.runs.append((tempname, self.count))
            self.postings = []
            self.size = 0
            self.count = 0
            
    def run_filenames(self):
        return [filename for filename, _ in self.runs]
    
    def cancel(self):
        self.cleanup()
    
    def cleanup(self):
        shutil.rmtree(self._dir)
    
    def _readback_lengths(self):
        for fieldnum, arry in self.length_arrays.iteritems():
            for docnum in xrange(len(arry)):
                byte = arry[docnum]
                if byte != 0:
                    yield fieldnum, docnum, byte
    
    def _lengths_array(self, doccount):
        full = array("B")
        for fieldnum in sorted(self.length_arrays.keys()):
            arry = self.length_arrays[fieldnum]
            if len(arry) < doccount:
                for _ in xrange(doccount - len(arry)):
                    arry.append(0)
            full.extend(arry)
            del self.length_arrays[fieldnum]
        return full
    
    def _finish_lengths(self, lengthfile, doccount):
        lengths = self._lengths_array(doccount)
        lengthfile.write_array(lengths)
        lengthfile.close()
        return lengths
    
    def _write_lengths_to(self, lengthfile, doccount):
        lengths = self._lengths_array(doccount)
        lengthfile.write_array(lengths)
        lengthfile.close()
    
    def finish(self, doccount, lengthfile, termtable, postingwriter):
        lengtharray = self._finish_lengths(lengthfile, doccount)
        print "la=", lengtharray
        lengths = MemoryLengthReader(lengtharray, doccount,
                                     self.schema.scorable_fields())
        
        if self.postings and len(self.runs) == 0:
            self.postings.sort()
            postiter = iter(self.postings)
            #total = len(self.postings)
        elif not self.postings and not self.runs:
            postiter = iter([])
            #total = 0
        else:
            postiter = imerge([read_run(runname, count)
                               for runname, count in self.runs])
            #total = sum(count for runname, count in self.runs)
        
        
        write_postings(self.schema, termtable, lengths, postingwriter, postiter)
        self.cleanup()
        

# Multiprocessing

class PoolWritingTask(Process):
    def __init__(self, dir, postingqueue, resultqueue, limitmb):
        Process.__init__(self)
        self.dir = dir
        self.postingqueue = postingqueue
        self.resultqueue = resultqueue
        self.limitmb = limitmb
        
    def run(self):
        pqueue = self.postingqueue
        rqueue = self.resultqueue
        
        subpool = TempfilePool(None, limitmb=self.limitmb, dir=self.dir,
                               basename=self.name)
        
        while True:
            code, args = pqueue.get()
            
            if code == -1:
                doccount = args
                break
            if code == 0:
                subpool.add_content(*args)
            elif code == 1:
                subpool.add_posting(*args)
            elif code == 2:
                subpool.add_field_length(*args)
        
        lenfilename = subpool._filename(self.name + "_lengths")
        subpool._write_lengths_to(StructFile(open(lenfilename, "wb")), doccount)
        subpool.dump_run()
        rqueue.put((subpool.runs, subpool.fieldlength_totals(),
                    subpool.fieldlength_maxes(), lenfilename))


class MultiPool(PoolBase):
    def __init__(self, schema, procs=2, limitmb=32, **kw):
        dir = tempfile.mkdtemp(".whoosh")
        PoolBase.__init__(self, schema, dir)
        
        self.procs = procs
        self.limitmb = limitmb
        
        self.postingqueue = Queue()
        self.resultsqueue = Queue()
        self.tasks = [PoolWritingTask(self._dir, self.postingqueue,
                                      self.resultsqueue, self.limitmb)
                      for _ in xrange(procs)]
        for task in self.tasks:
            task.start()
    
    def add_content(self, *args):
        self.postingqueue.put((0, args))
        
    def add_posting(self, *args):
        self.postingqueue.put((1, args))
    
    def add_field_length(self, *args):
        self.postingqueue.put((2, args))
    
    def cancel(self):
        for task in self.tasks:
            task.terminate()
        self.cleanup()
    
    def cleanup(self):
        shutil.rmtree(self._dir)
    
    def finish(self, doccount, lengthfile, termtable, postingwriter):
        _fieldlength_totals = self._fieldlength_totals
        if not self.tasks:
            return
        
        pqueue = self.postingqueue
        rqueue = self.resultsqueue
        
        for _ in xrange(self.procs):
            pqueue.put((-1, doccount))
        
        print "Joining..."
        t = now()
        for task in self.tasks:
            task.join()
        print "Join:", now() - t
        
        print "Getting results..."
        t = now()
        runs = []
        lenfilenames = []
        for task in self.tasks:
            taskruns, flentotals, flenmaxes, lenfilename = rqueue.get()
            runs.extend(taskruns)
            lenfilenames.append(lenfilename)
            for fieldnum, total in flentotals.iteritems():
                _fieldlength_totals[fieldnum] += total
            for fieldnum, length in flenmaxes.iteritems():
                if length > self._fieldlength_maxes.get(fieldnum, 0):
                    self._fieldlength_maxes[fieldnum] = length
        print "Results:", now() - t
        
        print "Writing lengths..."
        t = now()
        scorables = self.schema.scorable_fields()
        lenarray = array("B", (0 for _ in xrange(doccount * len(scorables))))
        
        for lenfilename in lenfilenames:
            sublengths = LengthReader.load(StructFile(open(lenfilename, "rb")),
                                           doccount, scorables)
            for i, byte in enumerate(sublengths.lengths):
                if byte != 0: lenarray[i] = byte
        lenwriter = LengthWriter(StructFile(lengthfile), doccount, scorables,
                                 lengths=lenarray)
        lenwriter.close()
        lengths = MemoryLengthReader(lenarray, doccount, scorables)
        print "Lengths:", now() - t
        
        t = now()
        iterator = dividemerge([read_run(runname, count)
                                for runname, count in runs])
        total = sum(count for runname, count in runs)
        write_postings(self.schema, termtable, lengths, postingwriter, iterator)
        print "Merge:", now() - t
        
        self.cleanup()
        


if __name__ == "__main__":
    pass
    



