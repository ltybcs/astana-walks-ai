# Presentation and video sources

The current presentation has 10 slides, editable PowerPoint text and budget table, a matching PDF and a speech script for about 6:20. The audience version focuses on the product and contains no internal release history or test reports in the spoken notes.

With the bundled artifact-tool runtime, start the presentation/PDF edit operation markers, copy `output/current/finalize-only.mjs` to `tmp/audience-revision/finalize-only.mjs`, then run:

```powershell
node output/current/build_current.mjs .
python output/current/build_current_pdf.py .
```

The builder writes a fresh candidate under `tmp/audience-revision/deliverable`. Preserve any existing stage before rebuilding. All ten slides and PDF pages were visually inspected. `audience-submission-check.json` binds the final files.

The video is a silent 84-second montage of five actual browser states. `video-manifest.json` and `video-review.json` bind the screenshots and checked MP4. The category-refusal scene is excluded. The full AI text is not expanded in the montage. YouTube publication is separate.

To rebuild the video, use `python output/current/build_current_video.py output/current/video-manifest.json --output tmp/rebuilt-demo.mp4 --ffmpeg-path <official-ffmpeg-executable>`.
