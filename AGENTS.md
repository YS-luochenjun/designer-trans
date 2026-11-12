# Project agents

Read `docs/architecture/route-work.md` when changing dispatch, conversations,
stage transitions, user questions, or rework. Read
`docs/operations/stage-quality.md` when changing cleanup, lettering, review,
or delivery readiness. Budget changes follow `docs/operations/resource-budget.md`.

Route owns user communication and bounded coordination. Work owns production
in its assigned domain. Handoffs contain factual reports, artifact identities,
and directions. Independent audio and visual branches may overlap after their
prerequisites.
One failed method or source does not establish a whole-task stop. Route assesses
the remaining scopes and available alternatives before concluding that work cannot continue.

Develop with isolated fixtures. The running Manager and Worker own production.
Preserve original media, unresolved requests and costs, frozen resources, and
local configuration. Load new code through controlled maintenance, retaining
an explicit user pause.

Only a verified clean plate unlocks lettering. Diagnostic exports retain their
unpassed quality state. Final human audiovisual approval binds the actual video SHA.
