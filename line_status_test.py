from line_status import classify


def test_free_when_below_limit():
    assert classify(0, 1, 0) == "free"
    assert classify(1, 2, 0) == "free"


def test_mine_when_full_with_own_streams():
    assert classify(1, 1, 1) == "mine"


def test_busy_when_someone_else_streams():
    assert classify(1, 1, 0) == "busy"
    assert classify(2, 2, 1) == "busy"


def test_no_limit_is_free():
    assert classify(3, 0, 0) == "free"
