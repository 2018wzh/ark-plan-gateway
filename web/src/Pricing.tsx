import {useEffect, useState} from 'react';
import {Alert, AutoComplete, Button, Card, InputNumber, Space, Table, Typography, message} from 'antd';
import {DeleteOutlined, PlusOutlined, SaveOutlined} from '@ant-design/icons';
import {api,errorText} from './api';

type Price={input?:number,output?:number};
type PricingData={default:Price,models:Record<string,Price>};
export default function Pricing({models,onDirty}:{models:string[],onDirty:(value:boolean)=>void}){
  const [data,setData]=useState<PricingData>({default:{},models:{}}),[saved,setSaved]=useState<PricingData>();
  const [name,setName]=useState(''),[saving,setSaving]=useState(false),[loading,setLoading]=useState(true),[error,setError]=useState('');
  const [msg,holder]=message.useMessage();
  const dirty=!!saved&&JSON.stringify(saved)!==JSON.stringify(data);
  useEffect(()=>{onDirty(dirty);return()=>onDirty(false)},[dirty,onDirty]);
  const load=async()=>{setLoading(true);try{const value=await api('/pricing');setData(value);setSaved(value);setError('')}catch(e){setError(errorText(e))}finally{setLoading(false)}};
  useEffect(()=>{void load()},[]);
  const setRate=(model:string|null,side:'input'|'output',value:number|null)=>setData(old=>{
    const price={...(model===null?old.default:old.models[model])};
    if(value===null)delete price[side];else price[side]=value;
    return model===null?{...old,default:price}:{...old,models:{...old.models,[model]:price}};
  });
  const rateInput=(model:string|null,side:'input'|'output',value?:number)=><InputNumber aria-label={`${model||'默认'}${side==='input'?'输入':'输出'}价格`} min={0} precision={6} value={value??null} disabled={loading||saving||!saved} placeholder={model===null?'未配置':'使用默认定价'} onChange={v=>setRate(model,side,v)} style={{width:'100%',minWidth:135}}/>;
  const add=()=>{const model=name.trim();if(!model){msg.warning('请选择或输入模型名称');return}if(model.length>128){msg.warning('模型名称最多 128 个字符');return}if(Object.hasOwn(data.models,model)){msg.warning('模型已存在');return}setData(old=>({...old,models:{...old.models,[model]:{}}}));setName('')};
  const save=async()=>{if(saving||!dirty)return;setSaving(true);try{const value=await api('/pricing','PUT',data);setData(value);setSaved(value);setError('');msg.success('定价已保存')}catch(e){setError(errorText(e))}finally{setSaving(false)}};
  return <div className="page-stack">{holder}
    {error&&<Alert type="error" showIcon message={error} action={!saved?<Button size="small" onClick={load}>重试</Button>:undefined}/>}
    <Card title="默认定价" extra={<Typography.Text type="secondary">人民币 / 百万 Token</Typography.Text>} loading={loading}>
      <div className="price-defaults"><label>输入 Token{rateInput(null,'input',data.default.input)}</label><label>输出 Token{rateInput(null,'output',data.default.output)}</label></div>
      <Typography.Paragraph className="field-note" type="secondary">未单独设置价格的模型使用此单价。留空表示未定价，填写 0 表示免费。</Typography.Paragraph>
    </Card>
    <Card title="模型专属定价" styles={{body:{padding:0}}}>
      <div className="table-toolbar"><AutoComplete aria-label="定价模型名称" value={name} onChange={setName} placeholder="选择已有模型或输入名称" className="price-model-input" options={models.filter(model=>!Object.hasOwn(data.models,model)).map(value=>({value}))} filterOption={(input,option)=>String(option?.value).toLowerCase().includes(input.toLowerCase())} disabled={loading||saving||!saved} onKeyDown={e=>{if(e.key==='Enter')add()}}/><Button icon={<PlusOutlined/>} onClick={add} disabled={loading||saving||!saved}>添加模型</Button></div>
      <Table size="middle" pagination={false} loading={loading} scroll={{x:620}} dataSource={Object.entries(data.models).map(([model,price])=>({key:model,model,...price}))} locale={{emptyText:'未配置专属价格，所有模型使用默认定价'}} columns={[
        {title:'模型',dataIndex:'model',width:250,render:value=><code>{value}</code>},
        {title:'输入 · 元/百万 Token',render:(_,row)=>rateInput(row.model,'input',row.input)},
        {title:'输出 · 元/百万 Token',render:(_,row)=>rateInput(row.model,'output',row.output)},
        {title:'操作',width:70,render:(_,row)=><Button type="text" danger aria-label={`删除 ${row.model} 定价`} icon={<DeleteOutlined/>} disabled={saving} onClick={()=>setData(old=>{const models={...old.models};delete models[row.model];return {...old,models}})}/>},
      ]}/>
      <div className="table-footer">专属价格留空时，输入和输出分别继承默认单价。</div>
    </Card>
    <div className="save-bar"><div><b>{dirty?'有未保存的修改':'当前定价已保存'}</b><span>等效价格按已报告 Token 与当前单价估算。</span></div><Space><Button disabled={!dirty||saving} onClick={()=>{if(saved)setData(saved);setError('')}}>撤销修改</Button><Button type="primary" icon={<SaveOutlined/>} loading={saving} disabled={!dirty||loading} onClick={save}>保存定价</Button></Space></div>
  </div>;
}
