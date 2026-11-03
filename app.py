"""탤2짱 CLIP STUDIO — mobile-friendly 20-second video maker."""
from __future__ import annotations
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import secrets
from concurrent.futures import ThreadPoolExecutor
from PIL import ImageStat, ImageFilter
from pydantic import BaseModel, Field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
JOBS = Path(os.getenv('JOBS_DIR', str(ROOT / 'jobs')))
JOBS.mkdir(parents=True, exist_ok=True)
STATIC = ROOT / 'static'
MAX_FILES = 18
MAX_TOTAL_BYTES = 400 * 1024 * 1024
JOB_TTL_SECONDS = 24 * 3600
STATUS: dict[str, dict] = {}
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=1)
MAX_JOBS = int(os.getenv('MAX_JOBS', '4'))
ADMISSION = threading.BoundedSemaphore(MAX_JOBS)
app = FastAPI(title='탤2짱 CLIP STUDIO')
app.mount('/static', StaticFiles(directory=STATIC), name='static')

PHOTO_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'}
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.webm'}
AUDIO_EXT = {'.mp3', '.m4a', '.wav', '.aac'}


def ffmpeg(*args):
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-filter_complex_threads', '1', '-threads', '2', *map(str, args)]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if run.returncode:
        raise RuntimeError((run.stderr or 'ffmpeg failed')[-1200:])


def video_duration(path: Path) -> float:
    run = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                         capture_output=True, text=True, timeout=20)
    try:
        return float(run.stdout.strip())
    except ValueError:
        return 0.0


def font_path() -> str:
    candidates = [
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf',
        '/usr/share/fonts/truetype/noto/NotoSansCJKkr-Regular.otf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for f in candidates:
        if Path(f).exists():
            return f
    import glob
    matches = glob.glob('/usr/share/fonts/**/*Sans*CJK*', recursive=True)
    if matches:
        return matches[0]
    return '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'


def wrap_text(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.FreeTypeFont, max_width: int):
    lines = []
    for paragraph in value.split('\n'):
        if not paragraph.strip():
            continue
        line = ''
        for ch in paragraph:
            test = line + ch
            if line and draw.textbbox((0, 0), test, font=font)[2] > max_width:
                lines.append(line.strip())
                line = ch
            else:
                line = test
        if line:
            lines.append(line.strip())
    return lines[:3]


def make_caption(text: str, width: int, height: int, path: Path):
    overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.truetype(font_path(), max(26, int(width * 0.047)))
    lines = wrap_text(draw, text, font, int(width * 0.82))
    if lines:
        spacing = int(width * 0.025)
        line_height = int(width * 0.070)
        total_height = len(lines) * line_height + 2 * spacing
        y0 = int(height * 0.72) - total_height // 2
        draw.rounded_rectangle((int(width * 0.06), y0 - 18, int(width * 0.94), y0 + total_height + 18),
                               radius=20, fill=(15, 15, 15, 132))
        for i, line in enumerate(lines):
            box = draw.textbbox((0, 0), line, font=font)
            x = (width - (box[2] - box[0])) // 2
            draw.text((x, y0 + i * line_height), line, font=font, fill=(255, 255, 255, 255),
                      stroke_width=1, stroke_fill=(0, 0, 0, 100))
    overlay.save(path)


def prepare_photo(path: Path, target: Path, width: int, height: int):
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert('RGB')
        img = ImageOps.fit(img, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.45))
        img.save(target, quality=94)


def make_segment(src: Path, out: Path, caption: Path, seconds: float, width: int, height: int, index: int, start: float = 0):
    frames = max(1, round(seconds * 24))
    if src.suffix.lower() in PHOTO_EXT:
        still = out.with_suffix('.jpg')
        prepare_photo(src, still, width, height)
        # Small Ken Burns movement, no black bars.
        vf = (f"zoompan=z='min(zoom+0.00055,1.09)':"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
              f"d={frames}:s={width}x{height}:fps=24,format=yuv420p")
        ffmpeg('-loop', '1', '-framerate', '1', '-i', still, '-i', caption,
               '-filter_complex', f'[0:v]{vf}[base];[base][1:v]overlay=0:0:format=auto,scale=in_range=auto:out_range=tv,format=yuv420p,setparams=range=limited,fade=t=in:d=0.167,fade=t=out:st={max(0, seconds-0.167):.6f}:d=0.167[v]',
               '-map', '[v]', '-frames:v', frames, '-an', '-c:v', 'libx264',
               '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p', '-r', '24',
               '-color_range', 'tv', '-movflags', '+faststart', out)
    else:
        length = video_duration(src)
        if length < seconds:
            start = 0.0
        vf = (f'scale={width}:{height}:force_original_aspect_ratio=increase,'
              f'crop={width}:{height},setsar=1,fps=24,format=yuv420p')
        ffmpeg('-stream_loop', '-1', '-ss', f'{start:.3f}', '-i', src, '-i', caption,
               '-filter_complex', f'[0:v]{vf}[base];[base][1:v]overlay=0:0:format=auto,scale=in_range=auto:out_range=tv,format=yuv420p,setparams=range=limited,fade=t=in:d=0.167,fade=t=out:st={max(0, seconds-0.167):.6f}:d=0.167[v]',
               '-map', '[v]', '-frames:v', frames, '-an', '-c:v', 'libx264',
               '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p', '-r', '24',
               '-color_range', 'tv', '-movflags', '+faststart', out)


def save_status(job_id, value):
    with LOCK:
        STATUS[job_id] = value
        target = JOBS / job_id / 'status.json'
        tmp = target.with_suffix('.tmp')
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        tmp.replace(target)


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def frame_score(image):
    gray = image.convert('L').resize((180, 320))
    stats = ImageStat.Stat(gray)
    sharp = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0]
    return sharp + stats.stddev[0] * .15 - abs(stats.mean[0] - 128) * .08


def analyze(src, work):
    if src.suffix.lower() in PHOTO_EXT:
        with Image.open(src) as image:
            image = ImageOps.exif_transpose(image)
            return frame_score(image), 0., 0.
    duration = video_duration(src)
    if not math.isfinite(duration) or duration <= 0 or duration > 1800:
        raise ValueError('영상은 재생 가능한 30분 이하 파일을 사용해 주세요')
    best = (-1e9, 0.)
    previous = None
    for fraction in (.12, .32, .52, .72, .88):
        position = max(0., duration * fraction - .5)
        sample = work / 'sample.jpg'
        ffmpeg('-ss', position, '-i', src, '-frames:v', '1', '-vf', 'scale=180:320:force_original_aspect_ratio=decrease', sample)
        with Image.open(sample) as image:
            score = frame_score(image)
            current = image.convert('L').resize((32, 32))
            if previous is not None:
                from PIL import ImageChops
                score += min(12, ImageStat.Stat(ImageChops.difference(current, previous)).mean[0] * .3)
            previous = current
        if score > best[0]:
            best = (score, position)
    return best[0], best[1], duration


def render(job_id, media, captions, title, audio, quality, plan=None):
    work = JOBS / job_id
    width, height = (1080, 1920) if quality == 'high' else (720, 1280)
    try:
        if plan is None:
            candidates = []
            for i, src in enumerate(media):
                save_status(job_id, {'status':'analyzing', 'progress':round(i / len(media) * 15), 'message':f'장면 분석 {i+1}/{len(media)}'})
                score, position, duration = analyze(src, work)
                candidates.append((i, score, position, duration))
            chosen = sorted(sorted(candidates, key=lambda item:item[1], reverse=True)[:10])
            base, extra = divmod(480, len(chosen))
            plan = []
            for i, (original, score, position, duration) in enumerate(chosen):
                frames = base + (i < extra)
                text = captions[min(i * len(captions)//len(chosen), len(captions)-1)]
                plan.append({'source':media[original].name, 'original_index':original+1, 'frames':frames,
                             'start':max(0, min(position, duration-frames/24)) if duration else 0, 'caption':text})
            (work / 'project.json').write_text(json.dumps({'plan':plan, 'quality':quality, 'title':title,
                'audio':audio.name if audio else None}, ensure_ascii=False), encoding='utf-8')
        parts = []
        for i, scene in enumerate(plan):
            png = work / f'caption_{i:02d}.png'
            make_caption(scene['caption'], width, height, png)
            seg = work / f'seg_{i:02d}.mp4'
            make_segment(work / scene['source'], seg, png, scene['frames']/24, width, height, i, scene['start'])
            parts.append(seg)
            save_status(job_id, {'status':'rendering','progress':15+round((i+1)/len(plan)*75),'message':f'장면 {i+1}/{len(plan)} 편집 중'})
        concat = work / 'concat.txt'
        concat.write_text(''.join(f"file '{part.name}'\n" for part in parts), encoding='utf-8')
        silent = work / 'silent.mp4'
        ffmpeg('-f','concat','-safe','0','-i',concat,'-c','copy','-movflags','+faststart',silent)
        output = work / 'clip_20sec.tmp.mp4'
        if audio:
            ffmpeg('-i',silent,'-stream_loop','-1','-i',audio,'-map','0:v:0','-map','1:a:0',
                   '-c:v','copy','-c:a','aac','-b:a','160k','-af','afade=t=in:st=0:d=0.5,afade=t=out:st=19:d=1',
                   '-t','20','-movflags','+faststart',output)
        else:
            shutil.copy2(silent, output)
        output.replace(work / 'clip_20sec.mp4')
        save_status(job_id, {'status':'done','progress':100,'message':'완성!', 'url':f'/download/{job_id}',
                            'preview':f'/preview/{job_id}?v={time.time_ns()}', 'scenes':plan})
    except Exception:
        import logging
        logging.exception('Render failed: %s', job_id)
        save_status(job_id, {'status':'error','progress':0,'message':'파일을 처리하지 못했어요. 손상된 파일이나 지원하지 않는 코덱인지 확인하고 다시 시도해 주세요.'})

    finally:
        ADMISSION.release()

def cleanup():
    now = time.time()
    for folder in JOBS.iterdir():
        if folder.is_dir() and now - folder.stat().st_mtime > JOB_TTL_SECONDS and STATUS.get(folder.name, {}).get('status') not in ('queued','analyzing','rendering'):
            shutil.rmtree(folder, ignore_errors=True)
            with LOCK:
                STATUS.pop(folder.name, None)


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')


@app.post('/api/create')
async def create(files: list[UploadFile] = File(...), title: str = Form('나의 솔직 후기'),
                 captions: str = Form(''), quality: str = Form('high'),
                 music: Optional[UploadFile] = File(None)):
    cleanup()
    with LOCK:
        if sum(v.get('status') in ('queued','analyzing','rendering') for v in STATUS.values()) >= MAX_JOBS:
            raise HTTPException(429, '편집 대기열이 가득 찼어요. 잠시 후 다시 시도해 주세요')
    if shutil.disk_usage(JOBS).free < 3 * 1024**3:
        raise HTTPException(503, '서버 저장 공간 정리 중입니다. 잠시 후 다시 시도해 주세요')
    if not 1 <= len(files) <= MAX_FILES:
        raise HTTPException(400, f'사진·영상은 1~{MAX_FILES}개까지 선택해 줘')
    if quality not in ('standard', 'high'):
        raise HTTPException(400, '화질 선택 오류')
    job_id = secrets.token_hex(24)
    work = JOBS / job_id
    if not ADMISSION.acquire(blocking=False):
        raise HTTPException(429, '편집 대기열이 가득 찼어요. 잠시 후 다시 시도해 주세요')
    work.mkdir()
    media = []
    total = 0
    try:
        for i, uploaded in enumerate(files):
            ext = Path(uploaded.filename or '').suffix.lower()
            if ext not in PHOTO_EXT | VIDEO_EXT:
                raise HTTPException(400, f'지원하지 않는 파일 형식: {ext or "알 수 없음"}')
            dest = work / f'input_{i:02d}{ext}'
            with dest.open('wb') as f:
                while chunk := await uploaded.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_TOTAL_BYTES:
                        raise HTTPException(413, '총 파일 크기는 400MB 이하로 선택해 줘')
                    f.write(chunk)
            media.append(dest)
            await uploaded.close()
        audio = None
        if music and music.filename:
            ext = Path(music.filename).suffix.lower()
            if ext not in AUDIO_EXT:
                raise HTTPException(400, '음악은 MP3, M4A, WAV, AAC만 지원해')
            audio = work / f'music{ext}'
            with audio.open('wb') as f:
                while chunk := await music.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_TOTAL_BYTES:
                        raise HTTPException(413, '총 파일 크기는 400MB 이하로 선택해 줘')
                    f.write(chunk)
        clean_title = title.strip()[:80] or '나의 솔직 후기'
        lines = [s.strip()[:65] for s in captions.splitlines() if s.strip()][:12]
        if not lines:
            lines = [clean_title, '일상에서 만난 작은 순간', '눈길이 머무는 디테일', '장소·가격·사용 팁을 여기에 적어 주세요', '다시 꺼내 보고 싶은 기록']
        with LOCK:
            STATUS[job_id] = {'status': 'queued', 'progress': 0, 'message': '편집 준비 중'}
        save_status(job_id, STATUS[job_id])
        POOL.submit(render, job_id, media, lines, clean_title, audio, quality)
        return {'job_id': job_id}
    except Exception:
        ADMISSION.release()
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        for uploaded in files:
            await uploaded.close()
        if music:
            await music.close()


@app.get('/api/status/{job_id}')
def status(job_id: str):
    with LOCK:
        validate_id(job_id)
        item = STATUS.get(job_id)
        if item is None and (JOBS / job_id / 'status.json').exists():
            item = load_json(JOBS / job_id / 'status.json')
            if item['status'] in ('queued','analyzing','rendering'):
                item = {'status':'error','progress':0,'message':'서버가 재시작됐어요. 파일을 다시 선택해 제작해 주세요.'}
    if item is None:
        raise HTTPException(404, '작업을 찾을 수 없어')
    return JSONResponse(item, headers={'Cache-Control':'no-store'})


@app.get('/preview/{job_id}')
def preview(job_id: str):
    validate_id(job_id)
    p = JOBS / job_id / 'clip_20sec.mp4'
    if not p.exists():
        raise HTTPException(404, '영상 준비 중')
    return FileResponse(p, media_type='video/mp4', headers={'Accept-Ranges': 'bytes'})


@app.get('/download/{job_id}')
def download(job_id: str):
    validate_id(job_id)
    p = JOBS / job_id / 'clip_20sec.mp4'
    if not p.exists():
        raise HTTPException(404, '영상 준비 중')
    return FileResponse(p, media_type='video/mp4', filename='tal2_clip_20sec.mp4')


def validate_id(job_id):
    if not re.fullmatch(r'[a-f0-9]{48}', job_id):
        raise HTTPException(404, '작업을 찾을 수 없어요')


class CaptionEdit(BaseModel):
    captions: list[str] = Field(min_length=1, max_length=10)


@app.post('/api/edit/{job_id}')
def edit(job_id: str, body: CaptionEdit):
    validate_id(job_id)
    with LOCK:
        if json.loads(status(job_id).body)['status'] not in ('done','error'):
            raise HTTPException(409, '현재 편집이 진행 중이에요')
        project_path = JOBS / job_id / 'project.json'
        if not project_path.exists():
            raise HTTPException(404, '수정할 프로젝트가 없어요')
        project = load_json(project_path)
        if len(body.captions) != len(project['plan']) or any(len(s)>65 for s in body.captions):
            raise HTTPException(400, '장면별 자막을 각각 65자 이하로 입력해 주세요')
        for scene, caption in zip(project['plan'], body.captions):
            scene['caption'] = caption.strip()
        if not ADMISSION.acquire(blocking=False):
            raise HTTPException(429, '편집 대기열이 가득 찼어요. 잠시 후 다시 시도해 주세요')
        project_path.write_text(json.dumps(project, ensure_ascii=False), encoding='utf-8')
        save_status(job_id, {'status':'queued','progress':0,'message':'수정한 자막으로 다시 제작 중'})
        audio = JOBS / job_id / project['audio'] if project['audio'] else None
        POOL.submit(render, job_id, [], [], project['title'], audio, project['quality'], project['plan'])
    return {'job_id':job_id}


@app.get('/healthz')
def health():
    return {'status':'ok', 'ffmpeg':bool(shutil.which('ffmpeg')), 'heic': __import__('importlib.util', fromlist=['find_spec']).find_spec('pillow_heif') is not None}


@app.on_event('startup')
def startup():
    def janitor():
        while True:
            cleanup()
            time.sleep(600)
    threading.Thread(target=janitor, daemon=True).start()
