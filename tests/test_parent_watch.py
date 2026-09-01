import threading

from lostpath import parent_watch


def test_watch_is_disabled_without_valid_parent_pid():
    assert parent_watch.start_from_environment({}) is False
    assert parent_watch.start_from_environment({parent_watch.PARENT_PID_ENV: "bad"}) is False


def test_watch_exits_after_parent_disappears():
    checked = []
    exited = threading.Event()

    def alive(pid):
        checked.append(pid)
        return len(checked) == 1

    started = parent_watch.start_from_environment(
        {parent_watch.PARENT_PID_ENV: "4242"},
        interval=0.001,
        is_alive=alive,
        exit_process=lambda code: exited.set() if code == 0 else None,
    )

    assert started is True
    assert exited.wait(1)
    assert checked == [4242, 4242]
