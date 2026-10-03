import {useEffect,useState} from 'react';
import {Alert,Button,Card,Checkbox,Descriptions,Form,Input,Select,Space,Table,Tag,Typography} from 'antd';
import {ReloadOutlined} from '@ant-design/icons';
import {api,errorText} from './api';
import {Account} from './accounts';

type Attempt={attempt:number;account_id:string;plan:string;upstream_status:number|null;upstream_request_id:string;outcome:string;source:string;error_code:string;error_type:string;error_param:string;duration_ms:number};
type Entry=Omit<Attempt,'attempt'>&{id:number;request_id:string;created_at:number;method:string;path:string;transport:string;model:string;http_status:number|null;attempt_count:number;had_errors:number;attempts:Attempt[]};
type History={items:Entry[];next_cursor:number|null;total:number;recovered:number;summary:{source:string;error_code:string;count:number;last_seen:number}[];write_failures:number;retention_days:number;max_rows:number};
const sources:Record<string,string>={upstream:'上游',gateway:'网关',client:'客户端'};
const date=(stamp:number)=>new Date(stamp*1000).toLocaleString();

export default function AuditLog({accounts}:{accounts:Account[]}){
  const [filters,setFilters]=useState<Record<string,string>>({days:'7',errors_only:'true'});
  const [cursors,setCursors]=useState<(number|null)[]>([null]);
  const [history,setHistory]=useState<History|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[refresh,setRefresh]=useState(0);
  useEffect(()=>{
    let active=true;
    setBusy(true);setError('');
    const query=new URLSearchParams({...filters,limit:'50'});
    const cursor=cursors[cursors.length-1];if(cursor!==null)query.set('before',String(cursor));
    api(`/audit?${query}`).then((data:History)=>{if(active)setHistory(data)}).catch(e=>{if(active)setError(errorText(e))}).finally(()=>{if(active)setBusy(false)});
    return()=>{active=false};
  },[filters,cursors,refresh]);
  const accountName=(id:string)=>accounts.find(a=>a.id===id)?.label||id||'—';
  const outcome=(row:Entry)=>row.outcome==='success'?<Tag color={row.had_errors?'gold':'green'}>{row.had_errors?'切换后成功':'成功'}</Tag>:<Tag color={row.outcome==='disconnected'?'default':'red'}>{row.outcome==='disconnected'?'客户端断开':'错误'}</Tag>;
  return <Space direction="vertical" size="large" style={{width:'100%'}}>
    <Alert type="info" showIcon message="请求错误历史" description="记录推理请求的结果和账号尝试，包括 HTTP 200 中的流式错误。只保存诊断元数据，不保存请求正文、生成内容、密钥或原始错误消息。来源标记表示错误发生的位置，不代表已确认根因。"/>
    <Card>
      <Form layout="inline" initialValues={{days:'7',errors_only:true}} onFinish={values=>{
        const next:Record<string,string>={days:String(values.days),errors_only:String(!!values.errors_only)};
        for(const key of ['source','error_code','model','http_status','request_id'])if(values[key])next[key]=String(values[key]).trim();
        setHistory(null);setFilters(next);setCursors([null]);
      }} style={{gap:12}}>
        <Form.Item name="days" label="时间"><Select style={{width:110}} options={[{value:'1',label:'最近 1 天'},{value:'7',label:'最近 7 天'},{value:'30',label:'最近 30 天'}]}/></Form.Item>
        <Form.Item name="source" label="来源"><Select allowClear placeholder="全部" style={{width:105}} options={Object.entries(sources).map(([value,label])=>({value,label}))}/></Form.Item>
        <Form.Item name="error_code"><Input aria-label="完整错误码" placeholder="完整错误码" maxLength={128} allowClear/></Form.Item>
        <Form.Item name="model"><Input aria-label="模型" placeholder="模型" maxLength={128} allowClear/></Form.Item>
        <Form.Item name="http_status"><Select allowClear placeholder="HTTP 状态" style={{width:130}} options={[200,400,401,403,404,408,413,429,500,502,503,504].map(value=>({value:String(value),label:String(value)}))}/></Form.Item>
        <Form.Item name="request_id"><Input aria-label="网关审计 ID" placeholder="网关审计 ID" maxLength={128} allowClear/></Form.Item>
        <Form.Item name="errors_only" valuePropName="checked"><Checkbox>仅错误及切换恢复</Checkbox></Form.Item>
        <Button type="primary" htmlType="submit" loading={busy}>查询</Button>
        <Button icon={<ReloadOutlined/>} loading={busy} onClick={()=>setRefresh(n=>n+1)}>刷新</Button>
      </Form>
    </Card>
    {error&&<Alert type="error" showIcon message="日志查询失败" description={error}/>}
    {!!history?.write_failures&&<Alert type="warning" showIcon message={`本次启动有 ${history.write_failures} 次日志写入失败，历史可能不完整。`}/>}
    {history&&<Card title="错误分布" extra={`保留 ${history.retention_days} 天，最多 ${history.max_rows.toLocaleString()} 条`}>
      <Typography.Paragraph>当前筛选共 {history.total} 个请求，其中 {history.recovered} 个在账号切换后成功。以下按最终错误计数。</Typography.Paragraph>
      <Space wrap>{history.summary.length?history.summary.map(item=><Tag key={`${item.source}:${item.error_code}`} color="red">{sources[item.source]||item.source} · {item.error_code||'未提供错误码'} × {item.count}</Tag>):<Typography.Text type="secondary">没有最终错误</Typography.Text>}</Space>
    </Card>}
    <Card title="请求历史">
      <Table<Entry> rowKey="id" dataSource={history?.items||[]} loading={busy} pagination={false} scroll={{x:1150}} columns={[
        {title:'时间',dataIndex:'created_at',render:date},
        {title:'接口',render:(_,r)=><><div>{r.method} {r.path}</div><Typography.Text type="secondary">{r.transport.toUpperCase()}</Typography.Text></>},
        {title:'模型',dataIndex:'model',render:value=>value||'—'},
        {title:'结果',render:(_,r)=>outcome(r)},
        {title:'HTTP',dataIndex:'http_status',render:value=>value??'—'},
        {title:'来源',dataIndex:'source',render:value=>sources[value]||'—'},
        {title:'错误码',dataIndex:'error_code',render:value=>value||'—'},
        {title:'账号',dataIndex:'account_id',render:accountName},
        {title:'耗时',dataIndex:'duration_ms',render:value=>`${value} ms`},
      ]} expandable={{expandedRowRender:r=><Space direction="vertical" style={{width:'100%'}}>
        <Descriptions size="small" bordered column={2} items={[
          {key:'id',label:'网关审计 ID',children:<Typography.Text copyable>{r.request_id}</Typography.Text>},
          {key:'upstream',label:'上游请求 ID',children:r.upstream_request_id?<Typography.Text copyable>{r.upstream_request_id}</Typography.Text>:'未提供'},
          {key:'type',label:'错误类型',children:r.error_type||'未提供'},
          {key:'param',label:'错误参数',children:r.error_param||'未提供'},
        ]}/>
        <Typography.Text>共尝试 {r.attempt_count} 个账号；超过 16 次时保留前 15 次和最后一次。</Typography.Text>
        <Table<Attempt> size="small" rowKey="attempt" pagination={false} dataSource={r.attempts} scroll={{x:800}} columns={[
          {title:'尝试',dataIndex:'attempt'},{title:'账号',dataIndex:'account_id',render:accountName},
          {title:'套餐',dataIndex:'plan'},{title:'上游 HTTP',dataIndex:'upstream_status',render:value=>value??'—'},
          {title:'结果',dataIndex:'outcome'},{title:'来源',dataIndex:'source',render:value=>sources[value]||'—'},
          {title:'错误码',dataIndex:'error_code'},{title:'上游请求 ID',dataIndex:'upstream_request_id'},
          {title:'错误类型',dataIndex:'error_type'},{title:'错误参数',dataIndex:'error_param'},
          {title:'耗时',dataIndex:'duration_ms',render:value=>`${value} ms`},
        ]}/>
      </Space>}}/>
      <Space style={{marginTop:16}}><Button disabled={busy||cursors.length===1} onClick={()=>setCursors(p=>p.slice(0,-1))}>上一页</Button><span>第 {cursors.length} 页</span><Button disabled={busy||!history?.next_cursor} onClick={()=>setCursors(p=>[...p,history!.next_cursor])}>下一页</Button></Space>
    </Card>
  </Space>;
}
