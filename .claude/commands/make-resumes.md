---
description: Write, check, and rank resumes for the roles picked by text
argument-hint: "[role IDs, such as 214 219; default: every picked role]"
---

Make resumes for roles picked in the job finder. Arguments: $ARGUMENTS

## Setup

1. Read `private/server.env`. It sets `JOB_FINDER_HOST`, `JOB_FINDER_KEY`, `PROFILE`, and `MASTER_RESUME`.
2. Read the whole `PROFILE` file. It holds the candidate's facts, the eligibility rules, the resume steps, the honesty rules, and the LaTeX checks. Follow it, with these changes for this machine:
	- Save output under `resumes/<id>-<company-slug>/`, not `/mnt/user-data/outputs/`.
	- `MASTER_RESUME` holds every experience, project, skill, award, and interest, and runs about 3 pages on purpose. Build each one-page resume by copying its preamble and header, then choosing and trimming its sections, entries, bullets, and skill rows. Its bullets and skills count as confirmed facts alongside the profile. Its `%` comments still mean "unconfirmed" or "placeholder".
	- Compile with `pdflatex` (`/Library/TeX/texbin/pdflatex`; it may not be on `PATH`). If it is missing, use `tectonic`, which cannot read two pdfTeX-only lines, so in the tailored copy only, replace `\input{glyphtounicode}` with `\usepackage{iftex}\ifPDFTeX\input{glyphtounicode}\fi` and `\pdfgentounicode=1` with `\ifPDFTeX\pdfgentounicode=1\fi`. Never edit `MASTER_RESUME`. Check with `pdfinfo` and `pdftotext` as the profile's build section says, but use `pdftotext -layout X.pdf - | grep -E '^\s{4,}\S+(\s+\S+)?\s*$'` for spillover: the profile's pattern misses lines with two leftover words. The only line it should print is the name.
3. Get the picked roles:

	```bash
	ssh -i $JOB_FINDER_KEY ubuntu@$JOB_FINDER_HOST 'cd job_finder && ~/venv/bin/python finder.py picks'
	```

	If the arguments name role IDs, keep only those. If no role is picked, say so and stop.

## For each role

1. **Read the posting.** Fetch the role's `url` and save the posting text to `posting.md`. If the fetch fails or returns no job description, ask the user to paste the posting, and go on to the next role meanwhile.
2. **Check eligibility first**, as the profile's Step 1 says. If the candidate clearly cannot be considered, write the reason to `summary.md`, run `finder.py mark <id> ineligible "<reason>"` over SSH, and move on. If eligibility is uncertain, continue and flag it.
3. **Analyze the role** with the profile's Steps 2 to 8. Save the analysis to `analysis.md`.
4. **Write four resumes at once**, one subagent each. Give each subagent the posting, the analysis, the profile path, the `MASTER_RESUME` path, and its output path.
	- `A.tex`, `B.tex`, `C.tex`: the candidate's best possible resume for this role. Each one uses only facts from `MASTER_RESUME` and the profile, and follows the profile's honesty rules. Give each a different lead, so the three differ: A leads with the posting's technology stack, B with the company's domain and problem, and C with the trait the posting stresses most.
	- `D.tex`: a benchmark. It is the strongest realistic resume a different new graduate could send for this role, with a fictional name and no contact details. It must not use the candidate's facts, and its claims must be believable for a student.
	- Each subagent compiles its resume and fixes it until it fits one page, has no line with only one or two words spilled over, and has no empty gap from the template quirk. It reports the page count and the spillover check output.
5. **Fact-check A, B, and C yourself** against `MASTER_RESUME` and the profile. Every employer, title, date, number, tool, and outcome must appear in one of them. Move anything unconfirmed into a `% ADD ONLY IF TRUE` comment and recompile.
6. **Rank them blind.** Run `pdftotext` on all four PDFs, remove names and contact lines, shuffle them, and label them R1 to R4. Give one subagent only those four texts and the posting, with this brief:

	> You are a recruiter at a company that gets thousands of applications an hour. You skim each resume in seconds, but you look hard for specific, verifiable results that fit this job, and you reject anything generic. Score each resume from 0 to 100, decide `interview` or `reject`, and explain why in at most 3 short lines, naming the details that decided it. Then rank them. Most resumes you see get rejected, so score that way.

7. **Write `summary.md`**: the eligibility result, a table of A to D with score, decision, and the recruiter's reason (at most 3 lines each, as the recruiter wrote it), which resume to send, what D does that the candidate's resumes don't, every `% ADD ONLY IF TRUE` item, and the profile's gap and project recommendation.
8. **Record the result on the server**, with D labeled as the benchmark:

	```bash
	ssh -i $JOB_FINDER_KEY ubuntu@$JOB_FINDER_HOST 'cd job_finder && ~/venv/bin/python finder.py mark <id> "resumes ready" "send B (84, interview); A 71; C 66; benchmark 88"'
	```

## When every role is done

List each role with its folder, the resume to send, its score and decision, and any eligibility warning. Open the PDF to send for each role.
