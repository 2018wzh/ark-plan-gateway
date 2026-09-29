import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from gateway.pricing import price_period, price_usage
from gateway.store import Store


TIERS = [{"max_input_tokens": 32000, "input": 3.2, "output": 16},
         {"max_input_tokens": 128000, "input": 4.8, "output": 24},
         {"max_input_tokens": 256000, "input": 9.6, "output": 48}]
PRICING = {"default": {"input": 1, "output": 2}, "models": {"m": {"tiers": TIERS}}}


@pytest.mark.parametrize("length,rate", [(32000,3.2),(32001,4.8),(128000,4.8),(128001,9.6),(256000,9.6)])
def test_context_tier_boundaries(length, rate):
    result = price_usage(PRICING, "m", length, length, 0)
    assert result["equivalent_cny"] == pytest.approx(length * rate / 1_000_000)


@pytest.mark.parametrize("length", [-1,256001])
def test_unknown_or_out_of_range_does_not_fall_back_to_default(length):
    assert price_usage(PRICING, "m", length, 500, 40) == {
        "equivalent_cny": 0, "unpriced_input_tokens": 500, "unpriced_output_tokens": 40}


def test_per_request_tiers_survive_aggregation_and_restart(tmp_path):
    path=str(tmp_path / "usage.db")
    key=Fernet.generate_key().decode()
    store=Store(path,key)
    account=store.add_account("coding","test-key",models=["m"])
    store.set_pricing(PRICING)
    store.record_request(account,"success",10,"m",32000,1000)
    store.record_request(account,"success",20,"m",32001,1000)
    store.record_request(account,"success",20,"m",32000,2000)
    store.close()
    store=Store(path,key)
    row=store.statistics(None,1)["model_daily"][0]
    assert row["requests"] == 3 and row["input_tokens"] == 96001
    assert row["equivalent_cny"] == pytest.approx((64000*3.2+3000*16+32001*4.8+1000*24)/1_000_000)
    store.close()


def test_old_totals_preserved_but_not_assigned_a_context_tier(tmp_path):
    import time
    path=str(tmp_path / "old.db")
    db=sqlite3.connect(path)
    db.execute("CREATE TABLE request_daily(day TEXT,account_id TEXT,model TEXT,outcome TEXT,requests INTEGER,input_tokens INTEGER,output_tokens INTEGER,latency_ms INTEGER,PRIMARY KEY(day,account_id,model,outcome))")
    db.execute("INSERT INTO request_daily VALUES(?,?,?,?,?,?,?,?)",(time.strftime('%Y-%m-%d',time.gmtime()),'old','m','success',2,64001,1000,20))
    db.commit();db.close()
    store=Store(path,Fernet.generate_key().decode())
    store.set_pricing(PRICING)
    row=store.statistics(None,1)["model_daily"][0]
    assert row["requests"] == 2 and row["unpriced_input_tokens"] == 64001
    assert row["unpriced_output_tokens"] == 1000 and row["equivalent_cny"] == 0
    store.close()


def test_beijing_peak_time_edges_and_weekend():
    def at(value):return price_period(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())
    assert at('2026-09-28T00:59:59') == 0
    assert at('2026-09-28T01:00:00') == 1
    assert at('2026-09-28T04:00:00') == 0
    assert at('2026-09-28T06:00:00') == 1
    assert at('2026-09-28T10:00:00') == 0
    assert at('2026-09-27T01:00:00') == 0
    config={"default":{},"models":{"m":{"input":1,"output":4,"peak":{"input":2,"output":8}}}}
    assert price_usage(config,'m',1000,1000,1000,0)["equivalent_cny"] == .005
    assert price_usage(config,'m',1000,1000,1000,1)["equivalent_cny"] == .01
    assert price_usage(config,'m',-1,1000,1000,-1)["unpriced_input_tokens"] == 1000


def test_catalog_and_invalid_tier_configuration(monkeypatch):
    monkeypatch.setenv('ARK_GATEWAY_ALLOW_UNCONFIGURED','1')
    from gateway.main import ModelPrice, PricingIn
    catalog=json.loads((Path(__file__).parents[1]/'docs/public-pricing.json').read_text(encoding='utf-8'))
    assert len(PricingIn(default={},models=catalog['models']).models) == 15
    for bad in ({"tiers":list(reversed(TIERS))},{"tiers":[TIERS[0],TIERS[0]]},{"tiers":TIERS,"input":1},{"peak":{"input":1},"input":1,"output":2}):
        with pytest.raises(ValidationError):ModelPrice(**bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_proxy_prices_reported_model_and_full_context(tmp_path, monkeypatch, stream):
    import httpx
    monkeypatch.setenv('ARK_GATEWAY_ALLOW_UNCONFIGURED','1')
    monkeypatch.setenv('ARK_GATEWAY_ADMIN_PASSWORD','admin-password-123')
    monkeypatch.setenv('ARK_GATEWAY_SERVICE_TOKEN','service-token-123456789012345')
    from gateway.main import create_app
    store=Store(str(tmp_path/'proxy.db'),Fernet.generate_key().decode())
    store.add_account('coding','test-key',models=['ark-code-latest'])
    store.set_pricing(PRICING)
    response={"id":"resp_tier","model":"m","usage":{"input_tokens":32001,"output_tokens":1000}}
    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ('event: response.completed\ndata: '+json.dumps({'response':response})+'\n\n').encode()
    def upstream(request):
        return httpx.Response(200,stream=Events(),headers={'content-type':'text/event-stream'}) if stream else httpx.Response(200,json=response)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app=create_app(store,remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            result=await client.post('/v1/responses',headers={'authorization':'Bearer service-token-123456789012345'},json={'model':'ark-code-latest','input':'test','stream':stream})
            assert result.status_code == 200
    row=store.statistics(None,1)['model_daily'][0]
    assert row['model'] == 'm'
    assert row['equivalent_cny'] == pytest.approx((32001*4.8+1000*24)/1_000_000)
    store.close()
