---
title: Hello
date: 2026-10-14
tags: [meta]
---

Hello, world. This is the first post on the new site, mostly here to prove the pipeline works end to end: write Markdown, push to `main`, and GitHub Actions builds and publishes it.

## How it's built

The site is generated with [Hugo](https://gohugo.io/) and the PaperMod theme, then deployed to GitHub Pages.

```bash
hugo new content posts/my-next-post.md
hugo server -D
```

## What's next

More posts soon. Subscribe via [RSS](/index.xml) if you'd like to follow along.
