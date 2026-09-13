# State machine

The explicit phases are `analyze -> plan -> execute -> validate -> evaluate -> checkpoint`, with
rollback to the accepted checkpoint on rejection. `continue` keeps the current semantic step alive
without creating a checkpoint. `verify` is a separate final gate before `complete`; `ask_user`
leaves the run resumable, and exhaustion enters `failed`. Invalid transitions raise `ValueError`.

A finish candidate (a `finish` decision or an evaluator `finish_candidate`) enters `verify`. When
verification passes and the worktree still contains uncommitted changes, the runtime transitions
`verify -> checkpoint`, commits `gcae: verified final state`, updates `accepted_commit`, and only
then transitions `checkpoint -> complete`. This keeps the invariant that a completed run's accepted
commit equals its verified tree. When the worktree is already clean, `verify -> complete` is taken
directly. Failed verification returns to `plan` for replanning.

Terminal phases `complete` and `failed` have no outgoing transitions.
