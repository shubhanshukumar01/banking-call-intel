Banking call analytics · solution overview

# CallIntel: from call transcript to verified insight

CallIntel reads recorded sales and service calls and turns each one into the same structured record: why the customer called, which products came up and in what way, how the sales conversation ended, and whether compliance rules were followed. Every finding links back to the exact lines of the conversation, and customer personal data is masked before any AI model sees the text.

[End-to-end flow](#callintel-architecture)[One call, start to finish](#callintel-call-journey)[What the AI model sees](#callintel-privacy)[How every insight is checked](#callintel-verification)

## End-to-end flow

Seven steps, left to right. Raw personal data exists only in steps 1 and 2; everything after that works on masked text. The AI steps reach the model through one gateway, which can use a model on the bank's own servers or an approved cloud model. Redis connects the steps so many calls run in parallel and none is processed twice.

CallIntel end-to-end flow: call transcripts are cleaned and masked inside a raw-data zone, triaged, turned into grounded facts by AI, analysed by specialist agents with the bank knowledge base, verified against the transcript and delivered as one JSON per call to dashboards.

## One call, start to finish

Most of a long call is hold time, identity checks and small talk. Cleaning and triage remove it before any AI step, so the AI reads a fraction of the text. Numbers are from a synthetic test call on a laptop.

One 6,000-line call: 65.8 thousand tokens shrink to 12.1 thousand after cleaning; 6,001 lines become 1,882; 3 of 6 sections go to the AI; 17 AI calls; one verified JSON in 133 seconds on a laptop.

## What the AI model sees

Personal details are replaced with placeholders on the bank's own servers. The AI model only ever receives the right-hand version, and a final guard blocks any request that still contains personal data.

Privacy masking example: names, date of birth, card and mobile numbers in the raw transcript are replaced locally with placeholders such as CUST_NAME_1 and CARD_1 before any AI step.

## How every insight is checked

Every reported insight points to the exact transcript lines it came from. Claims that cannot be backed by the transcript are never reported as facts.

Verification: each AI claim must pass five checks: allowed values, real transcript line references, matching quotes, matching figures and an independent fact-checker. Failures get one retry, then are removed or flagged for human review; passing claims are reported with evidence.

Slide-ready copies of these diagrams are in the project folder `docs/diagrams/`: high-resolution `.png` images and editable `.svg` files (PowerPoint: Insert, Pictures; right-click, Convert to Shape to edit). Timings and counts are from synthetic test calls; production figures depend on the bank's servers and real call content.