from abc import ABC, abstractmethod
from typing import Any


class BaseInjector(ABC):
    # Contract flags, read by the worker's inject dispatch (control-plane/worker/worker/
    # inject_dispatch.py) and described in docs/scenario-inject-execution.md.
    #
    # touches_range_hosts: the injector's contract is to act on hosts in the range (send
    #   traffic, deliver mail, create accounts). The mock backend has no hosts, so these
    #   are recorded as "skipped" there instead of run. True by default: a new injector
    #   has to opt in to running without a real range.
    # execution_mode: what execute() actually does today. "simulated" means it builds
    #   synthetic records and touches nothing; every injector in this package is
    #   simulated as of 2026-10. One that delivers to real hosts must say "live".
    touches_range_hosts: bool = True
    execution_mode: str = "simulated"

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = params or {}

    @abstractmethod
    def execute(self, context: dict[str, Any]) -> dict[str, Any]: ...

    def validate_params(self) -> None:  # noqa: B027 — optional override
        pass
