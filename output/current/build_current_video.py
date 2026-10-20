"""Make a captioned MP4 from actual UI scenario screenshots, without fake UI.

Input JSON: {"title": "...", "scenes": [{"image": "...jpg", "caption": "...",
"seconds": 13}]}. Image paths are relative to the manifest. No audio is added.
This is a slideshow of recorded app states, not a continuous screen recording.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

ROOT=Path(__file__).resolve().parents[2]


def filter_path(path):
    return str(Path(path).resolve()).replace("\\","/").replace(":","\\:").replace("'","\\'")


def ffmpeg_path(explicit):
    if explicit:
        result=Path(explicit).resolve()
        if not result.is_file():raise ValueError("--ffmpeg-path does not exist")
        return str(result)
    result=shutil.which("ffmpeg")
    if result:return result
    local=ROOT/".local/python"
    if local.exists():sys.path.insert(0,str(local))
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError("ffmpeg is missing. Use --ffmpeg-path or install imageio-ffmpeg into .local/python.") from None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest",type=Path)
    parser.add_argument("--output",type=Path,default=ROOT/"output/astana-walks-demo.mp4")
    parser.add_argument("--ffmpeg-path")
    parser.add_argument("--font",type=Path,default=Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts/arial.ttf")
    args=parser.parse_args()
    manifest=json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    scenes=manifest.get("scenes",[])
    if not scenes:raise ValueError("Manifest must contain actual screenshot scenes.")
    if not args.font.is_file():raise ValueError("A font with Cyrillic support is required.")
    executable=ffmpeg_path(args.ffmpeg_path)
    build=ROOT/"tmp/demo-video";build.mkdir(parents=True,exist_ok=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    title=manifest.get("title","Astana Walks: сценарии интерфейса")
    title_file=build/"title.txt";title_file.write_text(title,encoding="utf-8")
    disclosure=build/"disclosure.txt"
    disclosure.write_text("Фактические состояния приложения. Смены кадров не показывают задержку ответа.",encoding="utf-8")
    clips=[];evidence=[]
    for i,scene in enumerate(scenes,1):
        image=(args.manifest.resolve().parent/scene["image"]).resolve()
        if not image.is_file():raise ValueError(f"Missing screenshot: {image}")
        seconds=float(scene.get("seconds",13))
        if not 3<=seconds<=60:raise ValueError("Each scene must last 3–60 seconds.")
        caption=str(scene["caption"]).strip()
        if not caption:raise ValueError("Every scene must have an accurate caption.")
        lines=textwrap.wrap(caption,width=74,break_long_words=False,break_on_hyphens=False)
        if len(lines)>2:raise ValueError("Caption too long: use at most two readable lines.")
        caption_file=build/f"caption-{i:02}.txt"
        caption_file.write_text(f"{i}/{len(scenes)}  "+"\n".join(lines),encoding="utf-8")
        clip=build/f"scene-{i:02}.mp4"
        font=filter_path(args.font)
        vf=("scale=1840:760:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:108:color=0xF4F0E5,setsar=1,"
            f"drawtext=fontfile='{font}':textfile='{filter_path(title_file)}':expansion=none:fontcolor=0x0A2D2E:fontsize=42:x=40:y=31,"
            f"drawtext=fontfile='{font}':textfile='{filter_path(caption_file)}':expansion=none:fontcolor=0x0A2D2E:fontsize=35:line_spacing=10:x=52:y=890,"
            f"drawtext=fontfile='{font}':textfile='{filter_path(disclosure)}':expansion=none:fontcolor=0x55716A:fontsize=22:x=52:y=1030")
        subprocess.run([executable,"-hide_banner","-loglevel","warning","-y","-loop","1","-framerate","25","-i",str(image),"-t",str(seconds),"-vf",vf,"-an","-c:v","libx264","-threads","2","-preset","fast","-crf","18","-pix_fmt","yuv420p","-movflags","+faststart",str(clip)],check=True)
        clips.append(clip)
        evidence.append({"image":scene["image"],"sha256":hashlib.sha256(image.read_bytes()).hexdigest(),"caption":caption,"seconds":seconds})
    concat=build/"concat.txt"
    def concat_quote(p):return str(p).replace("\\","/").replace("'","'\\''")
    concat.write_text("\n".join("file '"+concat_quote(p)+"'" for p in clips)+"\n",encoding="utf-8")
    subprocess.run([executable,"-hide_banner","-loglevel","warning","-y","-f","concat","-safe","0","-i",str(concat),"-c","copy","-movflags","+faststart",str(args.output.resolve())],check=True)
    if args.output.stat().st_size<1000:raise RuntimeError("MP4 output is empty.")
    receipt={"format":"1920x1080, H.264, 25 fps, silent","kind":"captioned actual UI scenario screenshots","duration_seconds":sum(s["seconds"] for s in evidence),"scenes":evidence}
    args.output.with_suffix(".evidence.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"{args.output}: {receipt['duration_seconds']} seconds, {len(evidence)} actual UI states")


if __name__=="__main__":main()
