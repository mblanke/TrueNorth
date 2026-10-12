import random
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
    #   A simulated injector's result never claims it delivered or created anything on a
    #   host; run_inject labels it ``simulated: True`` / ``evidence_class: "synthetic"``.
    touches_range_hosts: bool = True
    execution_mode: str = "simulated"

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = params or {}
        # Every random choice goes through self.rng. run_inject seeds it from the exercise
        # and inject (injectors.inject_seed), so a replay builds the same synthetic records;
        # a direct caller gets a fixed seed rather than the process-global random.
        self.rng = random.Random(0)

    @abstractmethod
    def execute(self, context: dict[str, Any]) -> dict[str, Any]: ...

    def validate_params(self) -> None:  # noqa: B027 — optional override
        pass
