# State machine

The explicit phases are `analyze -> plan -> execute -> validate -> evaluate -> checkpoint`, with rollback to the accepted checkpoint on rejection. `continue` keeps the current semantic step alive without creating a checkpoint. `verify` is a separate final gate before `complete`; `ask_user` leaves the run resumable, and exhaustion enters `failed`. Invalid transitions raise `ValueError`.
