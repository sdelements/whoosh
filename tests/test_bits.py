from nose.tools import assert_equal  #@UnresolvedImport

from whoosh.support.bitvector import Bits


def test_bit_basics():
    b = Bits()
    assert not b

    b.update([0, 2, 4, 6, 7])
    assert b
    assert_equal([(n in b) for n in range(10)],
                 [True, False, True, False, True, False, True, True, False,
                  False])

    b.add(9)
    assert 9 in b
    assert_equal(len(b), 6)

    assert_equal(list(b.invert(10)), [1, 3, 5, 8])

    b.remove(6)
    assert_equal(list(b), [0, 2, 4, 7, 9])
    assert_equal(len(b), 5)

def test_len():
    b = Bits()
    b.add(3)
    b.add(5)
    b.add(1024)
    assert_equal(len(b), 3)
    b.add(5)
    assert_equal(len(b), 3)
    b.remove(1000)
    assert_equal(len(b), 3)
    b.remove(5)
    assert_equal(len(b), 2)

def test_union():
    assert_equal(Bits([2, 4, 5]) | Bits([3, 9]), Bits([2, 3, 4, 5, 9]))
    b = Bits([2, 4, 5])
    b.update([3, 9])
    assert_equal(list(b), [2, 3, 4, 5, 9])
    b = Bits([2, 4, 5])
    b.update(Bits([3, 9]))
    assert_equal(list(b), [2, 3, 4, 5, 9])
    b = Bits([1, 2])
    b.update([1, 5, 9])
    assert_equal(list(b), [1, 2, 5, 9])

def test_intersection():
    assert_equal(Bits([2, 4, 5]) & Bits([3, 9]), Bits())
    assert_equal(Bits([2, 4, 5]) & Bits([4, 5, 9]), Bits([4, 5]))
    b = Bits([2, 4, 5])
    assert_equal(b.intersection([4, 5, 9]), Bits([4, 5]))
    b.intersection_update([4, 5, 9])
    assert_equal(list(b), [4, 5])
    b = Bits([2, 4, 5])
    b.intersection_update(Bits([4, 5, 9]))
    assert_equal(list(b), [4, 5])

def test_difference():
    assert_equal(Bits([1, 3, 50, 72]) - Bits([3, 72]), Bits([1, 50]))
    assert_equal(list(Bits([1, 3, 50, 72]).difference([3, 72])), [1, 50])
    b = Bits([1, 3, 50, 72])
    b.difference_update(Bits([3, 72]))
    assert_equal(list(b), [1, 50])
    b = Bits([1, 3, 50, 72])
    b.difference_update([3, 72])
    assert_equal(list(b), [1, 50])

def test_xor():
    assert_equal(Bits([2, 4, 5]) ^ Bits([4, 5, 9]), Bits([2, 9]))

def test_copy():
    b = Bits([1, 5, 100, 60])
    assert_equal(b, b.copy())

def test_clear():
    b = Bits([1, 5, 100, 60])
    b.clear()
    assert_equal(list(b), [])

def test_isdisjoint():
    b = Bits([1, 7, 20, 100])
    assert b.isdisjoint(Bits([2, 8, 25]))
    assert b.isdisjoint([2, 8, 25])
    assert not b.isdisjoint(Bits([2, 7, 25]))
    assert not b.isdisjoint([1, 8, 25])

def test_before_after():
    b = Bits([10, 11, 30, 50, 80])
    assert_equal(b.after(0), 10)
    assert_equal(b.after(7), 10)
    assert_equal(b.after(8), 10)
    assert_equal(b.after(10), 11)
    assert_equal(b.after(11), 30)
    assert_equal(b.after(30), 50)
    assert_equal(b.after(33), 50)
    assert_equal(b.after(38), 50)
    assert_equal(b.after(41), 50)
    assert_equal(b.after(42), 50)
    assert_equal(b.after(45), 50)
    assert_equal(b.after(47), 50)
    assert_equal(b.after(50), 80)
    assert_equal(b.after(80), None)

    assert_equal(b.before(0), None)
    assert_equal(b.before(99), 80)
    assert_equal(b.before(81), 80)
    assert_equal(b.before(80), 50)
    assert_equal(b.before(50), 30)
    assert_equal(b.before(48), 30)
    assert_equal(b.before(46), 30)
    assert_equal(b.before(45), 30)
    assert_equal(b.before(44), 30)
    assert_equal(b.before(42), 30)
    assert_equal(b.before(38), 30)
    assert_equal(b.before(36), 30)
    assert_equal(b.before(34), 30)
    assert_equal(b.before(33), 30)
    assert_equal(b.before(32), 30)
    assert_equal(b.before(30), 11)
    assert_equal(b.before(11), 10)
    assert_equal(b.before(10), None)

    b = Bits([7])
    assert_equal(b.after(0), 7)
    b = Bits([8])
    assert_equal(b.after(0), 8)
    b = Bits([9])
    assert_equal(b.after(0), 9)

    b = Bits([7])
    assert_equal(b.before(16), 7)
    b = Bits([8])
    assert_equal(b.before(16), 8)
    b = Bits([9])
    assert_equal(b.before(16), 9)

    b = Bits([49])
    assert_equal(b.after(0), 49)
