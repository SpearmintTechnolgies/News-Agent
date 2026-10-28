"""Match working live proxy receipts: never invent provider billing or token usage."""
from __future__ import annotations
import base64
import io
import json
from PIL import Image
from newsagent_v2.image.image_proxy import Gx10ImageProxy
from newsagent_v2.v5_generation.depth_cost_helpers import accumulate_vertex_receipts


def test_live_proxy_success_preserves_unknown_billing(tmp_path,monkeypatch):
    buf=io.BytesIO()
    Image.new('RGB',(1024,1024),(20,40,60)).save(buf,format='PNG')
    body=json.dumps({'data':[{'b64_json':base64.b64encode(buf.getvalue()).decode()}]})
    monkeypatch.setattr(Gx10ImageProxy,'_post_once',lambda *args:(200,body))
    result=Gx10ImageProxy({'NEWSAGENT_IMAGE_PROXY_KEY':'synthetic-test'}).generate(
        type('Request',(),{'prompt':'hero'})(),dest_path=str(tmp_path/'out.png'))
    assert result.success
    assert result.provider_name=='image_proxy'
    assert result.provider_reported_cost_usd is None
    assert result.provider_reported_usage is None


def test_explicit_reported_receipts_are_accumulated(tmp_path):
    run=tmp_path/'evt-proxy-image_proxy'
    run.mkdir()
    (run/'provider_result.json').write_text(json.dumps({
        'provider_name':'image_proxy','model_name':'gemini-3.1-flash-image',
        'success':True,'provider_reported_cost_usd':0.067,
    }))
    image=accumulate_vertex_receipts('evt-proxy',tmp_path,{})
    assert image['requests']==1
    assert image['provider']=='image_proxy'
    assert image['provider_reported_cost_usd']==0.067
