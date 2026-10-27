---
title: "$title"
description: "Three sources, three answers: the model card, the leaderboard and the vendor blog each describe Q-7 differently. The desk reports all three."
date: $date
categories: [The Wire]
tags: [ai, news]
author: rhea
excerpt: "The model card, the leaderboard and the vendor blog agree that Q-7 exists. After that, the desk is on its own."
permalink: /wire/quokka-q7-e2e/
sources:
$sources_yaml
claims:
$claims_yaml
---
SYDNEY (The Wire) — Quokka Labs' Q-7 model ships with a context window of $context, according to the model's [model card]($url_model_card). On every other question the sources answer on their own schedules.

The public [leaderboard listing]($url_leaderboard) gives Q-7's license as $license. The company's [own blog]($url_vendor_blog) says the open weights arrive on $weights_date. The desk read each source at filing time and reports what each one said, not what each one meant.

Disclosure: this dispatch is a fixture from a local end-to-end test (revision $revision). Quokka Labs and Q-7 are fictional, an AI persona drafted it, and it was never published.

## Sources

- Q-7 model card (fixture), [example.org/e2e/quokka-q7/model-card]($url_model_card), read $date. The context window.
- Public leaderboard listing (fixture), [example.org/e2e/leaderboard/q7]($url_leaderboard), read $date. The license listing.
- Quokka Labs blog (fixture), [example.org/e2e/quokka-labs/blog/q7]($url_vendor_blog), read $date. The open-weights date.
