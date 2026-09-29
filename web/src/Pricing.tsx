import {useEffect, useState} from 'react';
import {Alert, Button, Card, Input, InputNumber, Space, Table, Typography, message} from 'antd';

type Price = {input?:number,output?:number};
type PricingData = {default:Price,models:Record<string,Price>};
type Api = (path:string,method?:string,body?:unknown)=>Promise<any>;

export default function Pricing({api,models}:{api:Api,models:string[]}) {
  const [data,setData] = useState<PricingData>({default:{},models:{}});
  const [name,setName] = useState('');
  const [saving,setSaving] = useState(false);
  const [msg,holder] = message.useMessage();
  useEffect(()=>{api('/pricing').then(setData).catch(e=>msg.error(String(e)))},[api]);
  const setRate = (model:string|null,side:'input'|'output',value:number|null) => setData(old=>{
    const price = {...(model===null?old.default:old.models[model])};
    if(value===null)delete price[side];else price[side]=value;
    return model===null?{...old,default:price}:{...old,models:{...old.models,[model]:price}};
  });
  const input = (model:string|null,side:'input'|'output',value?:number) =>
    <InputNumber min={0} precision={6} style={{width:150}} value={value} placeholder={model===null?'未配置':'使用默认定价'} onChange={v=>setRate(model,side,v)}/>;
  const rows = Object.entries(data.models).map(([model,price])=>({key:model,model,...price}));
  const add = () => {const model=name.trim();if(!model)return;if(data.models[model]){msg.warning('模型已存在');return}setData(old=>({...old,models:{...old.models,[model]:{}}}));setName('')};
  const save = async()=>{setSaving(true);try{await api('/pricing','PUT',data);msg.success('定价已保存')}catch(e){msg.error(String(e))}finally{setSaving(false)}};
  return <Space direction="vertical" size={18} style={{width:'100%'}}>
    {holder}
    <Alert type="info" showIcon message="等效价格是估算值" description="按上游报告的输入和输出 Token 与此处配置的人民币单价计算。它不代表火山引擎实际账单；未配置单价的 Token 会单独标出，不按零价计算。"/>
    <Card title="默认定价" extra={<Typography.Text type="secondary">人民币 / 百万 Token</Typography.Text>}><Space wrap>输入 {input(null,'input',data.default.input)} 输出 {input(null,'output',data.default.output)}</Space></Card>
    <Card title="模型定价" extra={<Space><Input placeholder="模型名称" value={name} onChange={e=>setName(e.target.value)} onPressEnter={add} style={{width:220}}/><Button onClick={add}>添加</Button></Space>}>
      <Table size="small" pagination={false} dataSource={rows} columns={[
        {title:'模型',dataIndex:'model'},
        {title:'输入 · 元/百万 Token',render:(_:unknown,row:{model:string,input?:number})=>input(row.model,'input',row.input)},
        {title:'输出 · 元/百万 Token',render:(_:unknown,row:{model:string,output?:number})=>input(row.model,'output',row.output)},
        {title:'操作',render:(_:unknown,row:{model:string})=><Button danger size="small" onClick={()=>setData(old=>{const models={...old.models};delete models[row.model];return {...old,models}})}>删除</Button>},
      ]}/>
      <Typography.Text type="secondary">留空时逐项使用默认定价。当前路由模型：{models.join('、')||'无'}。</Typography.Text>
      <div style={{marginTop:20}}><Button type="primary" loading={saving} onClick={save}>保存定价</Button></div>
    </Card>
  </Space>;
}
