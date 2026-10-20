# Current unified presentation

10 slides, 16:9. Estimated talk: 6:20, about 804 Russian words, excluding questions. Native PowerPoint text and the budget table are editable; the PDF contains the exact slide renders.

The two assets are verbatim actual screenshots from source 0bb. Their original `.png` filenames contained JPEG bytes; copies here use the correct `.jpg` extension. Copyright and attribution remain visible. They illustrate that UI version and do not prove the newer pharmacy fixes.

Build with the bundled Node.js runtime and `@oai/artifact-tool`:

```powershell
node output/current/build_current.mjs .
python output/current/build_current_pdf.py .
```

The PPTX finalizer refuses to overwrite an existing deliverable. For a new build, use a fresh output folder or preserve the previous deliverable separately first. The builder expects the primary runtime under the current user's `.cache/codex-runtimes`; no provider requests are made. Operation markers from the presentations/PDF skills must be run before a new authoring operation. Rendering output is staged in `tmp/current-submission`, outside the deliverable folder.

`presentation-validation.json` records package, layout, native table, and font policy checks. `current-submission-check.json` binds file hashes and visual review. All ten slide PNGs and all ten Poppler PDF page renders were individually inspected for this delivery. This is a content/layout review, not a native PowerPoint execution test.

Historical pre-unified PDFs and MP4 remain separate. YouTube is not published; `ltybcs.com/demo` is an intended permanent destination, not a confirmed deployment. Latest source-bound unit count shown in this deck is 386/384/2 at 8d49; the five iframe checks are a separate later delta.
