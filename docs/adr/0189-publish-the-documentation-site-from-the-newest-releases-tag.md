# 0189. Publish the documentation site from the newest release's tag

- Status: Accepted
- Date: 2026-11-18
- Amends: [0152](0152-link-the-pages-the-code-names-to-the-releases-tag.md)

## Context

The 2.8 audit found (A28-DX-02) that the site `mkdocs.yml` describes is
built on every change and never published. CI's `docs-site` job keeps it
as an artifact for seven days. [0152](0152-link-the-pages-the-code-names-to-the-releases-tag.md)
says "It is not published", and points every link the code prints at
GitHub, under the release's tag.

So the docs are read on GitHub, which:

- drops a code block's title, the only place where the tutorial and the
  recipes named the file to create (A28-DX-01, fixed in the prose);
- shows `!!! note` admonitions as source;
- has no page for the API reference that `docs/hooks/site.py` generates
  from the docstrings of the 1,527 public names, so a reader opens the
  source;
- searches about 51,000 lines of docs only through code search.

The pages still say "On the documentation site, API reference shows the
docstring of each one" (`docs/tutorial.md`, `docs/api.md`).

What a publishing workflow must keep, as the others do
(`domainkit_tests/test_ci.py`): actions pinned to commits, the workflow's
token limited to reading, write access only on the job that uses it, and
no secret stored. zizmor audits every workflow in CI.

Options considered:

1. **GitHub Pages, the newest release only**, from a workflow that runs
   on a release tag: the official `actions/upload-pages-artifact` and
   `actions/deploy-pages` [1, 2, 3]. The deploy job needs `pages: write`
   and `id-token: write` only; it writes no branch, and needs no token
   stored. A tag the release refuses, a pre-release, or a patch of an
   older line publishes nothing, so the site shows the newest final
   release.
2. **GitHub Pages, a folder per version**, with `mike` on a `gh-pages`
   branch [4]. It keeps every release's site, and adds a version picker.
   `mike deploy --push` commits to the branch, so the job needs
   `contents: write`: a token that can write the repository. `mike` is a
   new dependency of the docs group, and the branch grows with every
   release.
3. **Read the Docs** [5]: builds every tag, with versions and search,
   outside this repository. It is another service and account to run, its
   builds are outside the workflows that `test_ci.py` and zizmor check,
   and its configuration is a file of its own.
4. **Keep the site unpublished.** GitHub stays the only reader.

## Decision

Option 1.

- `.github/workflows/docs.yml` runs on a pushed `v*` tag:
  - its `tag` job makes the three checks of `release.yml`'s `tag` job
    (the tag is the version, on a commit of `main`, the release commit);
    `test_ci.py` checks that they are the same steps;
  - `.github/scripts/newest_release.py` says whether the tag is a final
    release newer than every other final release; if not, the run ends
    there;
  - its `site` job builds the site as CI's `docs-site` job does, with
    `--strict` and the docs group alone, and uploads it as the Pages
    artifact;
  - its `deploy` job, the only one with write access (`pages: write`,
    `id-token: write`), deploys that artifact to the `github-pages`
    environment.
- `mkdocs.yml` names the site: `site_url: https://karimmattar.github.io/domainkit/`.
- The links the code prints stay as [0152](0152-link-the-pages-the-code-names-to-the-releases-tag.md)
  decided: under the release's tag, on GitHub. The tag holds every
  release's text, and the site holds the newest release's only. The
  README, `init`'s `docs_url`, and `domainkit docs` do not change.
- PyPI's "Documentation" link names the first page, `docs/index.md`,
  under the release's tag, not `docs/reference.md`.

## Consequences

- Once the owner turns Pages on, a release's site has search, rendered
  admonitions, block titles, and the API reference, at a stable address.
- The repository's owner sets it up once (`docs/versioning.md`, "One-time
  setup"): Pages' source set to GitHub Actions, and tags `v*` allowed to
  deploy to the `github-pages` environment, which Pages limits to the
  default branch at first. Until then, the `deploy` job of each tag's run
  fails. The release does not depend on it.
- A release's site replaces the previous one. Older releases' pages stay
  readable under their tags on GitHub, as the code's links show them.
- A patch of an older line (the security fixes of the last 1.x minor
  after 2.0) and a pre-release leave the site as it is.
- A later decision can add versions (option 2) without a change to what
  the code names, as [0152](0152-link-the-pages-the-code-names-to-the-releases-tag.md)
  foresaw.

## References

1. GitHub, using custom workflows with GitHub Pages:
   <https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages>
2. `actions/deploy-pages`:
   <https://github.com/actions/deploy-pages>
3. `actions/upload-pages-artifact`:
   <https://github.com/actions/upload-pages-artifact>
4. mike, versioned docs for MkDocs:
   <https://github.com/jimporter/mike>
5. Read the Docs, versions:
   <https://docs.readthedocs.io/en/stable/versions.html>
