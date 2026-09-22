"""状态机。只说"哪些迁移合法"，不管"谁触发"。"""
from .models import TaskState as S

_TRANSITIONS: dict[S, set[S]] = {
    S.SUBMITTED:      {S.WORKING, S.REJECTED, S.CANCELED, S.FAILED},
    S.WORKING:        {S.INPUT_REQUIRED, S.COMPLETED, S.FAILED, S.CANCELED},
    S.INPUT_REQUIRED: {S.WORKING, S.FAILED, S.CANCELED},   # ★ 回 working 是关键
    S.COMPLETED:      set(),
    S.FAILED:         set(),
    S.CANCELED:       set(),
    S.REJECTED:       set(),
}

TERMINAL = frozenset({S.COMPLETED, S.FAILED, S.CANCELED, S.REJECTED})


class IllegalTransition(Exception):
    def __init__(self, frm: S, to: S):
        super().__init__(f"illegal transition: {frm.value} -> {to.value}")
        self.frm, self.to = frm, to


def can_transition(frm: S, to: S) -> bool:
    return to in _TRANSITIONS.get(frm, set())


def assert_transition(frm: S, to: S) -> None:
    if not can_transition(frm, to):
        raise IllegalTransition(frm, to)


def is_terminal(state: S) -> bool:
    return state in TERMINAL
