import {useEffect, useState} from 'react';
import {errorText} from './api';
import {Alert, Card, Empty, Select, Space, Statistic, Table, Typography} from 'antd';
import {DollarOutlined, DownloadOutlined, BarChartOutlined, UploadOutlined} from '@ant-design/icons';
import {Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis} from 'recharts';

type Account = {id:string,label:string,plan:string};
type Usage = {input_tokens:number,output_tokens:number,equivalent_cny:number,unpriced_input_tokens:number,unpriced_output_tokens:number};
type ModelDay = Usage & {day:string,model:string};
type Data = {model_daily:ModelDay[],model_hourly:(Usage & {hour:string,model:string})[],hourly_started_at:number};
type Api = (path:string)=>Promise<Data>;

const zero = ():Usage => ({input_tokens:0,output_tokens:0,equivalent_cny:0,unpriced_input_tokens:0,unpriced_output_tokens:0});
const add = (sum:Usage,row:Usage) => {for(const key of Object.keys(sum) as (keyof Usage)[])sum[key]+=row[key];return sum};
const money = (value:number) => `¥${value.toFixed(6)}`;
const number = (value:number) => value.toLocaleString('zh-CN');
const compact = (value:number) => Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1}).format(value);
const modelName = (value:string) => value || '未记录模型（历史数据）';

export default function Statistics({accounts,api}:{accounts:Account[],api:Api}) {
  const [accountId,setAccountId] = useState<string>();
  const [model,setModel] = useState<string>();
  const [days,setDays] = useState(1);
  const [granularity,setGranularity] = useState<'hour'|'day'>('hour');
  const [data,setData] = useState<Data>({model_daily:[],model_hourly:[],hourly_started_at:0});
  const [error,setError] = useState('');
  const [loading,setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    const load = () => api(`/statistics?days=${days}${accountId?`&account_id=${encodeURIComponent(accountId)}`:''}`)
      .then(value => {if(alive){setData(value);setError('')}})
      .catch(reason => {if(alive)setError(errorText(reason))})
      .finally(()=>{if(alive)setLoading(false)});
    load();
    const timer = setInterval(load,15000);
    return () => {alive=false;clearInterval(timer)};
  },[accountId,days,api]);

  const source = granularity==='hour'?data.model_hourly.map(row=>({...row,day:row.hour})):data.model_daily;
  const periodLabel = granularity==='hour'?'每小时':'每日';
  const rows = source.filter(row=>model===undefined||row.model===model);
  const totals = rows.reduce((sum,row)=>add(sum,row),zero());
  const unpriced = totals.unpriced_input_tokens+totals.unpriced_output_tokens;
  const modelTotals = new Map<string,Usage>();
  const dailyTotals = new Map<string,Usage>();
  for(const row of rows){
    modelTotals.set(row.model,add(modelTotals.get(row.model)||zero(),row));
    dailyTotals.set(row.day,add(dailyTotals.get(row.day)||zero(),row));
  }
  const models = [...modelTotals].map(([model,usage])=>({model,...usage}))
    .sort((a,b)=>(b.input_tokens+b.output_tokens)-(a.input_tokens+a.output_tokens));
  const since = new Date(new Date(Date.now()-(days-1)*86400000).toISOString().slice(0,10)+'T00:00:00Z').getTime();
  const step = granularity==='hour'?3600000:86400000;
  const trend = Array.from({length:Math.floor((Date.now()-since)/step)+1},(_,index)=>{
    const stamp = new Date(since+index*step).toISOString();
    const day = granularity==='hour'?stamp.slice(0,13)+':00:00Z':stamp.slice(0,10);
    return {day,...(dailyTotals.get(day)||zero())};
  });
  const tickTime = (value:string)=>granularity==='hour'?`${value.slice(5,10)} ${value.slice(11,16)}`:value.slice(5);
  const hasTokens = totals.input_tokens+totals.output_tokens>0;
  const hasPricedTokens = totals.input_tokens+totals.output_tokens-unpriced>0;

  return <Space direction="vertical" size={18} className="usage-page">
    <Space wrap>
      <Select aria-label="统计账号" style={{width:230}} value={accountId} allowClear placeholder="全部账号" onChange={value=>{setAccountId(value);setModel(undefined)}} options={accounts.map(a=>({value:a.id,label:`${a.label} · ${a.plan==='agent'?'Agent':'Coding'}`}))}/>
      <Select aria-label="统计模型" style={{width:230}} value={model} allowClear placeholder="全部模型" onChange={setModel} options={[...new Set(source.map(row=>row.model))].sort().map(value=>({value,label:modelName(value)}))}/>
      <Select aria-label="统计时间" style={{width:130}} value={days} onChange={setDays} options={[1,7,30,90].map(value=>({value,label:value===1?'今天（UTC）':`近 ${value} 天`}))}/>
      <Select aria-label="统计粒度" style={{width:130}} value={granularity} onChange={value=>{setGranularity(value);setModel(undefined)}} options={[{value:'hour',label:'每小时'},{value:'day',label:'每日'}]}/>
    </Space>
    {granularity==='hour'&&data.hourly_started_at>0&&<Typography.Text type="secondary">小时记录始于 {new Date(data.hourly_started_at*1000).toISOString().replace('T',' ').slice(0,19)} UTC，按请求完成时间归档；更早的数据请切换每日视图。</Typography.Text>}
    {error && <Alert type="error" message={error}/>}
    <div className="stats stats-four usage-metrics">
      <Card loading={loading}><Statistic title={<Space><DownloadOutlined/>输入 Token</Space>} value={totals.input_tokens}/></Card>
      <Card loading={loading}><Statistic title={<Space><UploadOutlined/>输出 Token</Space>} value={totals.output_tokens}/></Card>
      <Card loading={loading}><Statistic title={<Space><BarChartOutlined/>总 Token</Space>} value={totals.input_tokens+totals.output_tokens}/></Card>
      <Card loading={loading}><Statistic title={<Space><DollarOutlined/>等效价格</Space>} value={unpriced&&!hasPricedTokens?'未定价':money(totals.equivalent_cny)}/><Typography.Text type={unpriced?'warning':'secondary'}>{unpriced?`${number(unpriced)} Token 未定价 · 金额为已定价部分`:'人民币 · 按当前配置单价估算'}</Typography.Text></Card>
    </div>
    <div className="usage-charts">
      <Card title={`${periodLabel} Token 消耗`} extra={<Typography.Text type="secondary">UTC</Typography.Text>}>
        {hasTokens?<div className="usage-chart" role="img" aria-label={`${periodLabel}输入和输出 Token 堆叠柱状图`}><ResponsiveContainer width="100%" height="100%">
          <BarChart data={trend} margin={{top:12,right:12,left:0,bottom:0}}>
            <CartesianGrid strokeDasharray="3 3" vertical={false}/><XAxis dataKey="day" tickFormatter={tickTime} minTickGap={24}/><YAxis tickFormatter={compact} width={58}/>
            <Tooltip formatter={(v)=>number(Number(v))}/><Legend/>
            <Bar dataKey="input_tokens" name="输入 Token" stackId="tokens" fill="#158f98" isAnimationActive={false}/>
            <Bar dataKey="output_tokens" name="输出 Token" stackId="tokens" fill="#9bc9d1" isAnimationActive={false}/>
          </BarChart>
        </ResponsiveContainer></div>:<div className="usage-chart-empty"><Empty description="暂无已报告的 Token 消耗"/></div>}
      </Card>
      <Card title={`${periodLabel}等效价格`} extra={<Typography.Text type="secondary">人民币 · UTC</Typography.Text>}>
        {hasPricedTokens?<div className="usage-chart" role="img" aria-label={`${periodLabel}等效价格折线图`}><ResponsiveContainer width="100%" height="100%">
          <LineChart data={trend} margin={{top:12,right:12,left:0,bottom:0}}>
            <CartesianGrid strokeDasharray="3 3" vertical={false}/><XAxis dataKey="day" tickFormatter={tickTime} minTickGap={24}/><YAxis tickFormatter={v=>`¥${Number(v).toLocaleString('zh-CN',{maximumFractionDigits:6})}`} width={82}/>
            <Tooltip formatter={(v)=>money(Number(v))}/><Legend/>
            <Line dataKey="equivalent_cny" name="已定价部分" stroke="#9664d8" strokeWidth={2} dot={false} activeDot={{r:4}} isAnimationActive={false}/>
          </LineChart>
        </ResponsiveContainer></div>:<div className="usage-chart-empty"><Empty description={hasTokens?'配置模型定价后显示价格趋势':'暂无价格统计'}/></div>}
      </Card>
    </div>
    <Card title="模型消耗明细" extra={<Typography.Text type="secondary">仅统计响应中已报告的 Token</Typography.Text>}>
      <Table size="small" rowKey="model" loading={loading} scroll={{x:750}} dataSource={models} pagination={{pageSize:10}} locale={{emptyText:'暂无模型消耗记录'}} columns={[
        {title:'模型',dataIndex:'model',render:modelName},
        {title:'输入 Token',dataIndex:'input_tokens',render:number,sorter:(a,b)=>a.input_tokens-b.input_tokens},
        {title:'输出 Token',dataIndex:'output_tokens',render:number,sorter:(a,b)=>a.output_tokens-b.output_tokens},
        {title:'总 Token',render:(_,row)=>number(row.input_tokens+row.output_tokens),sorter:(a,b)=>a.input_tokens+a.output_tokens-b.input_tokens-b.output_tokens},
        {title:'等效价格',render:(_,row)=>row.input_tokens+row.output_tokens===row.unpriced_input_tokens+row.unpriced_output_tokens&&(row.input_tokens+row.output_tokens)>0?'未定价':money(row.equivalent_cny),sorter:(a,b)=>a.equivalent_cny-b.equivalent_cny},
        {title:'未定价 Token',render:(_,row)=>number(row.unpriced_input_tokens+row.unpriced_output_tokens)},
      ]}/>
    </Card>
    <Card title={`${periodLabel}模型明细`} extra={<Typography.Text type="secondary">UTC · 输入 / 输出分别计数</Typography.Text>}>
      <Table size="small" rowKey={row=>`${row.day}/${row.model}`} loading={loading} scroll={{x:850}} dataSource={[...rows].sort((a,b)=>b.day.localeCompare(a.day)||a.model.localeCompare(b.model))} pagination={{pageSize:10,showSizeChanger:true,pageSizeOptions:[10,25,50]}} columns={[
        {title:granularity==='hour'?'小时（UTC）':'日期（UTC）',dataIndex:'day',render:(value:string)=>value.replace('T',' ').replace('Z',''),sorter:(a,b)=>a.day.localeCompare(b.day)},
        {title:'模型',dataIndex:'model',render:modelName},
        {title:'输入 Token',dataIndex:'input_tokens',render:number},
        {title:'输出 Token',dataIndex:'output_tokens',render:number},
        {title:'总 Token',render:(_,row)=>number(row.input_tokens+row.output_tokens)},
        {title:'等效价格',render:(_,row)=>row.input_tokens+row.output_tokens>0&&row.input_tokens+row.output_tokens===row.unpriced_input_tokens+row.unpriced_output_tokens?'未定价':money(row.equivalent_cny)},
        {title:'未定价 Token',render:(_,row)=>number(row.unpriced_input_tokens+row.unpriced_output_tokens)},
      ]}/>
    </Card>
  </Space>;
}
