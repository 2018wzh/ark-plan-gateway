import {useEffect, useState} from 'react';
import {errorText} from './api';
import {quotaColor} from './accounts';
import {Alert, Card, Collapse, Empty, Progress, Select, Space, Statistic, Table, Typography} from 'antd';
import {DollarOutlined, DownloadOutlined, BarChartOutlined, UploadOutlined} from '@ant-design/icons';
import {Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis} from 'recharts';

type Account = {id:string,label:string,plan:string};
type Usage = {input_tokens:number,output_tokens:number,equivalent_cny:number,unpriced_input_tokens:number,unpriced_output_tokens:number};
type ModelDay = Usage & {day:string,model:string};
type Quota = {quota_group:string,plan:string,window:string,quota:number,used:number,reset_time:number|null,observed_at:number|null,quota_error?:string|null};
type Data = {model_daily:ModelDay[],quota_current:Quota[],quota_history:Quota[]};
type Api = (path:string)=>Promise<Data>;

const zero = ():Usage => ({input_tokens:0,output_tokens:0,equivalent_cny:0,unpriced_input_tokens:0,unpriced_output_tokens:0});
const add = (sum:Usage,row:Usage) => {for(const key of Object.keys(sum) as (keyof Usage)[])sum[key]+=row[key];return sum};
const money = (value:number) => `¥${value.toFixed(4)}`;
const number = (value:number) => value.toLocaleString('zh-CN');
const compact = (value:number) => Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1}).format(value);
const modelName = (value:string) => value || '未记录模型（历史数据）';
const fmt = (value:number|null) => value ? new Date(value*1000).toLocaleString('zh-CN') : '—';
const progress = (row:Quota) => {
  if(row.quota<=0)return '—';
  const used=Math.min(100,Math.max(0,row.used/row.quota*100));
  return <span title="剩余额度"><Progress type="circle" percent={100-used} size={64} strokeWidth={8} strokeColor={quotaColor(100-used)} format={()=>`${(100-used).toFixed(1)}%`}/></span>;
};

export default function Statistics({accounts,api}:{accounts:Account[],api:Api}) {
  const [accountId,setAccountId] = useState<string>();
  const [model,setModel] = useState<string>();
  const [days,setDays] = useState(30);
  const [data,setData] = useState<Data>({model_daily:[],quota_current:[],quota_history:[]});
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

  const rows = data.model_daily.filter(row=>model===undefined||row.model===model);
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
  const trend = Array.from({length:days},(_,index)=>{
    const day = new Date(Date.now()-(days-1-index)*86400000).toISOString().slice(0,10);
    return {day,...(dailyTotals.get(day)||zero())};
  });
  const hasTokens = totals.input_tokens+totals.output_tokens>0;
  const hasPricedTokens = totals.input_tokens+totals.output_tokens-unpriced>0;
  const current = new Map<string,Quota&{groups:number}>();
  for(const item of data.quota_current){
    const key = `${item.plan}/${item.window}`;
    const prior = current.get(key);
    if(prior){prior.quota+=item.quota;prior.used+=item.used;prior.groups++;prior.reset_time=null;prior.observed_at=Math.min(prior.observed_at||0,item.observed_at||0);prior.quota_error=prior.quota_error||item.quota_error}
    else current.set(key,{...item,groups:1});
  }

  return <Space direction="vertical" size={18} className="usage-page">
    <Space wrap>
      <Select aria-label="统计账号" style={{width:230}} value={accountId} allowClear placeholder="全部账号" onChange={value=>{setAccountId(value);setModel(undefined)}} options={accounts.map(a=>({value:a.id,label:`${a.label} · ${a.plan==='agent'?'Agent':'Coding'}`}))}/>
      <Select aria-label="统计模型" style={{width:230}} value={model} allowClear placeholder="全部模型" onChange={setModel} options={[...new Set(data.model_daily.map(row=>row.model))].sort().map(value=>({value,label:modelName(value)}))}/>
      <Select aria-label="统计时间" style={{width:130}} value={days} onChange={setDays} options={[7,30,90].map(value=>({value,label:`近 ${value} 天`}))}/>
    </Space>
    {error && <Alert type="error" message={error}/>}
    <div className="stats stats-four usage-metrics">
      <Card loading={loading}><Statistic title={<Space><DownloadOutlined/>输入 Token</Space>} value={totals.input_tokens}/></Card>
      <Card loading={loading}><Statistic title={<Space><UploadOutlined/>输出 Token</Space>} value={totals.output_tokens}/></Card>
      <Card loading={loading}><Statistic title={<Space><BarChartOutlined/>总 Token</Space>} value={totals.input_tokens+totals.output_tokens}/></Card>
      <Card loading={loading}><Statistic title={<Space><DollarOutlined/>等效价格</Space>} value={unpriced&&!hasPricedTokens?'未定价':money(totals.equivalent_cny)}/><Typography.Text type={unpriced?'warning':'secondary'}>{unpriced?`${number(unpriced)} Token 未定价 · 金额为已定价部分`:'人民币 · 按当前配置单价估算'}</Typography.Text></Card>
    </div>
    <div className="usage-charts">
      <Card title="每日 Token 消耗" extra={<Typography.Text type="secondary">UTC</Typography.Text>}>
        {hasTokens?<div className="usage-chart" role="img" aria-label="每日输入和输出 Token 堆叠柱状图"><ResponsiveContainer width="100%" height="100%">
          <BarChart data={trend} margin={{top:12,right:12,left:0,bottom:0}}>
            <CartesianGrid strokeDasharray="3 3" vertical={false}/><XAxis dataKey="day" tickFormatter={v=>v.slice(5)} minTickGap={24}/><YAxis tickFormatter={compact} width={58}/>
            <Tooltip formatter={(v)=>number(Number(v))}/><Legend/>
            <Bar dataKey="input_tokens" name="输入 Token" stackId="tokens" fill="#158f98" isAnimationActive={false}/>
            <Bar dataKey="output_tokens" name="输出 Token" stackId="tokens" fill="#9bc9d1" isAnimationActive={false}/>
          </BarChart>
        </ResponsiveContainer></div>:<div className="usage-chart-empty"><Empty description="暂无已报告的 Token 消耗"/></div>}
      </Card>
      <Card title="每日等效价格" extra={<Typography.Text type="secondary">人民币 · UTC</Typography.Text>}>
        {hasPricedTokens?<div className="usage-chart" role="img" aria-label="每日等效价格折线图"><ResponsiveContainer width="100%" height="100%">
          <LineChart data={trend} margin={{top:12,right:12,left:0,bottom:0}}>
            <CartesianGrid strokeDasharray="3 3" vertical={false}/><XAxis dataKey="day" tickFormatter={v=>v.slice(5)} minTickGap={24}/><YAxis tickFormatter={v=>`¥${compact(v)}`} width={58}/>
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
    <Collapse items={[{key:'quota',label:'官方额度与查询历史（按账号统计）',children:<Space direction="vertical" size={18} style={{width:'100%'}}>
      <Typography.Text type="secondary">共享额度主体已去重；Coding Plan 多主体显示平均比例。模型筛选仅影响 Token 与价格。</Typography.Text>
      <Table size="small" rowKey={row=>`${row.plan}/${row.window}`} pagination={false} scroll={{x:800}} dataSource={[...current.values()]} locale={{emptyText:'尚无官方额度数据'}} columns={[
        {title:'套餐',dataIndex:'plan',render:value=>value==='agent'?'Agent Plan':'Coding Plan'},
        {title:'窗口',dataIndex:'window'},{title:'额度主体',dataIndex:'groups'},
        {title:'剩余比例',render:(_,row)=>progress(row)},
        {title:'重置时间',dataIndex:'reset_time',render:fmt},
        {title:'更新时间',dataIndex:'observed_at',render:fmt},
        {title:'查询状态',dataIndex:'quota_error',render:value=>value?<Typography.Text type="warning">旧值 · {value}</Typography.Text>:'正常'},
      ]}/>
      <Table size="small" rowKey={row=>`${row.quota_group}/${row.window}/${row.observed_at}`} dataSource={data.quota_history} pagination={{pageSize:8}} locale={{emptyText:'暂无额度查询记录'}} scroll={{x:800}} columns={[
        {title:'查询时间',dataIndex:'observed_at',render:fmt},
        {title:'额度主体',dataIndex:'quota_group',render:(value:string)=>accounts.find(account=>account.id===value)?.label||value.slice(0,8)},
        {title:'窗口',dataIndex:'window'},
        {title:'剩余比例',render:(_,row)=>progress(row)},
        {title:'重置时间',dataIndex:'reset_time',render:fmt},
      ]}/>
    </Space>}]}/>
  </Space>;
}
