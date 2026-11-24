#!/usr/bin/env bash
# Usage: make_clip.sh SRC OUT "TOP LINE" "BOTTOM LINE" LOGO start-end [start-end ...]
# Cuts the given segments (seconds), joins them, makes 1080x1920 vertical with blurred background,
# top text and a logo near the bottom.
set -e
src=$1 out=$2 t1=$3 t2=$4 logo=$5; shift 5
font="C\\:/Windows/Fonts/arialbd.ttf"
f="" ; n=0
for seg in "$@"; do
  s=${seg%-*}; e=${seg#*-}
  f+="[0:v]trim=$s:$e,setpts=PTS-STARTPTS[v$n];[0:a]atrim=$s:$e,asetpts=PTS-STARTPTS[a$n];"
  n=$((n+1))
done
for ((i=0;i<n;i++)); do f+="[v$i][a$i]"; done
f+="concat=n=$n:v=1:a=1[v][a];"
f+="[v]split[bg][fg];[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:2[bg2];"
f+="[fg]scale=1080:-2[fg2];[bg2][fg2]overlay=(W-w)/2:(H-h)/2[base];"
f+="[1:v]scale=420:-1[lg];[base][lg]overlay=(W-w)/2:H-h-260[wl];"
f+="[wl]drawtext=fontfile='$font':text='$t1':fontsize=62:fontcolor=white:borderw=6:bordercolor=black:x=(w-tw)/2:y=330,"
f+="drawtext=fontfile='$font':text='$t2':fontsize=54:fontcolor=yellow:borderw=6:bordercolor=black:x=(w-tw)/2:y=415[outv]"
ffmpeg -y -v error -i "$src" -i "$logo" -filter_complex "$f" -map "[outv]" -map "[a]" \
  -c:v libx264 -preset medium -crf 20 -c:a aac -b:a 160k -movflags +faststart "$out"
