"""The live class: who is in an exercise and how each Student is doing (issue #199, R1 F2).

One read model for the Ops Center live view: the roster (``aar_access.participants`` plus
Students with their own lab on the exercise's range, the relations that already decide who
reads the AAR and who may submit a detection), each Student's team, own lab session, detection
submissions and the objectives an instructor's judgement credited them for (ADR 0020), and
"needs attention" items limited to what the data says today: a lab or range fault, and no
activity seen for a while. Help requests and interventions are the F2 model;
nothing here invents them.
"""
