import io
import json
import time
import subprocess
from pathlib import Path
import pytest
from PIL import Image
import pillow_heif
from fastapi.testclient import TestClient
import app as studio

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, 'JOBS', tmp_path)
    studio.STATUS.clear()
    return TestClient(studio.app)

def photo():
    out=io.BytesIO()
    Image.new('RGB',(600,800),'#71968a').save(out,'JPEG')
    return out.getvalue()

def wait(client, job):
    for _ in range(600):
        state=client.get('/api/status/'+job).json()
        if state['status'] in ('done','error'):
            assert state['status']=='done', state
            return state
        time.sleep(.25)
    pytest.fail('render timeout')

def test_validation(client):
    assert client.post('/api/create',files=[('files',('x.jpg',photo(),'image/jpeg'))]*19).status_code==400
    assert client.post('/api/create',files={'files':('bad.exe',b'x')}).status_code==400
    assert client.get('/api/status/invalid').status_code==404
    assert client.get('/healthz').json()['heic'] is True

def test_heic_mov_render_and_edit(client,tmp_path):
    heic=tmp_path/'source.heic'
    pillow_heif.from_pillow(Image.new('RGB',(600,800),'#af9781')).save(heic)
    mov=tmp_path/'source.MOV'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x480:rate=24','-t','4','-c:v','libx265','-threads','2','-x265-params','pools=1:frame-threads=1','-tag:v','hvc1','-y',str(mov)],check=True,capture_output=True)
    response=client.post('/api/create',files=[('files',('photo.HEIC',heic.read_bytes(),'image/heic')),('files',('video.MOV',mov.read_bytes(),'video/quicktime'))],data={'title':'오늘의 발견','quality':'high','captions':'작은 순간의 기록\n장소와 이용 팁을 적어 주세요'})
    assert response.status_code==200,response.text
    job=response.json()['job_id']; state=wait(client,job)
    target=studio.JOBS/job/'clip_20sec.mp4'
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(target)]))
    stream=probe['streams'][0]
    assert (stream['width'],stream['height'],stream['codec_name'],stream['pix_fmt'])==(1080,1920,'h264','yuv420p')
    assert float(probe['format']['duration'])==20
    assert int(stream['nb_frames'])==480
    response=client.get('/preview/'+job,headers={'Range':'bytes=0-1023'})
    assert response.status_code==206 and len(response.content)==1024
    assert client.get('/download/'+job).headers['content-disposition'].startswith('attachment')
    assert client.post('/api/edit/'+job,json={'captions':['수정한 자막','가격과 위치 확인']}).status_code==200
    assert wait(client,job)['scenes'][0]['caption']=='수정한 자막'
    studio.STATUS.clear()
    assert client.get('/api/status/'+job).json()['status']=='done'

def test_all_eighteen_analyzed(client,monkeypatch):
    seen=[]
    def analyze(src,work):
        index=int(src.stem.split('_')[-1]);seen.append(index);return index,0,0
    monkeypatch.setattr(studio,'analyze',analyze)
    def segment(src,out,*args):out.write_bytes(b'mock')
    monkeypatch.setattr(studio,'make_segment',segment)
    def ffmpeg(*args):Path(args[-1]).write_bytes(b'mock')
    monkeypatch.setattr(studio,'ffmpeg',ffmpeg)
    response=client.post('/api/create',files=[('files',(f'{i}.jpg',photo(),'image/jpeg')) for i in range(18)])
    state=wait(client,response.json()['job_id'])
    assert seen==list(range(18))
    assert [s['original_index'] for s in state['scenes']]==list(range(9,19))
    assert sum(s['frames'] for s in state['scenes'])==480
