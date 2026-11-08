# Observe — <what to find out>
<!--
For a --read-only run, usually with --schema: the worker cannot write, and its
final message is the deliverable (answerPath; answer.json with --schema). Copy
this file, fill in every section, pass it with --task. Comments like this one
never reach the worker.
-->

## Question
<The one thing to find out, phrased so the answer settles it.>

## Where to look
<Files and line ranges, commands that only read, or, with --computer-use, the
apps and screens. Not "the codebase".>

## Answer
<What the final message must hold. With --schema, say what each required key
means; delegate appends the schema itself.>

## Out of bounds
<What the worker must not do: commands that write, network, other apps.>
