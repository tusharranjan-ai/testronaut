# testronaut-review

## Description
Review and approve requirements or test cases for a generation run.

## Usage
```
/testronaut-review <run-id> [options]
```

## Options
- `--gate <1|2>` — Which gate to review (1=requirements, 2=test cases)
- `--action <approve|reject|edit>` — Bulk action (default: interactive)

## Behavior
Loads the run and presents findings from the review agent.

### Gate 1 (Requirements)
- Shows each requirement with its review findings
- For each finding: field, severity, message, proposed fix
- User can: accept fix, reject fix, edit manually, skip

### Gate 2 (Test Cases)
- Shows each test case with its review findings
- Shows traceability matrix (requirements ↔ cases)
- User can: accept fix, reject fix, edit manually, skip

## Output
Updates artifact status in DB:
- `approved` — user accepted
- `edited` — user modified
- `draft` — user rejected (needs regeneration)

After both gates pass, run status becomes `done` and artifacts are exportable.
