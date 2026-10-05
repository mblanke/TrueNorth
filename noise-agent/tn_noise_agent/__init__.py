"""tn-noise: the TrueNorth background-noise agent.

Runs inside a range VM as white-cell infrastructure. It is out of bounds for students:
it takes orders only from the controller over the management NIC, and generates its
traffic on the training NIC, where it is meant to be seen.

Standard library only, so it can be dropped onto an air-gapped image as-is (and frozen
with PyInstaller for Windows).
"""

VERSION = "0.1.0"
