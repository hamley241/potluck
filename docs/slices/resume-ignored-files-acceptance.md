A1. `potluck fix --resume <run-dir>` succeeds in a repo that carries ignored files (a `.venv/`,
    a `.env`, or both). It does not refuse on their presence.

A2. After such a resume, every ignored file that existed before it STILL EXISTS and its contents
    are unchanged, byte for byte. Proven by a test that writes known content, resumes, and reads
    it back.

A3. The refusals protecting TRACKED work are unchanged and still fire: a dirty tree, a base
    mismatch, a submodule, an incomplete capture, and either restore-digest mismatch each still
    refuse. Covered by the existing suites, which must pass unedited for these cases.

A4. No path in the resume restore can delete an ignored file. If a `clean -x` remains anywhere on
    the restore path, the change is wrong.

A5. `real_restore_tree` in `harness/orchestrator.py` is byte-unchanged.

A6. The code states explicitly what the restore does and does not guarantee about ignored paths,
    in the same register as the sibling's docstring — a reader must not have to infer the new
    boundary.

A7. A new test resumes in a repo CARRYING an ignored file and asserts both that the resume
    proceeds and that the file survives intact. It fails if the old delete-and-refuse behaviour
    is restored.

A8. Any existing test edited is named in the commit with the reason, and only where it pinned the
    old behaviour directly. No test is edited to make an unrelated failure pass.

A9. `./.claude/verify.sh` exits 0.
