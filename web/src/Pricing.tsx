import {useEffect, useState} from 'react';
import {Alert, AutoComplete, Button, Card, InputNumber, Space, Table, Typography, message} from 'antd';
import {DeleteOutlined, PlusOutlined, SaveOutlined} from '@ant-design/icons';
import {api,errorText} from './api';

type Tier={max_input_tokens:number,input:number,output:number};
type Price={input?:number,output?:number,tiers?:Tier[],peak?:{input:number,output:number},note?:string};
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
    if(price.note&&!price.note.startsWith('已手动修改'))price.note='已手动修改单价；'+price.note;
    return model===null?{...old,default:price}:{...old,models:{...old.models,[model]:price}};
  });
  const rateInput=(model:string|null,side:'input'|'output',value?:number)=><InputNumber aria-label={`${model||'默认'}${side==='input'?'输入':'输出'}价格`} min={0} precision={6} value={value??null} disabled={loading||saving||!saved} placeholder={model===null?'未配置':'使用默认定价'} onChange={v=>setRate(model,side,v)} style={{width:'100%',minWidth:135}}/>;
  const conditionalInput=(model:string,side:'input'|'output',price:Price)=>{
    const entries=price.tiers?.length?price.tiers.map((tier,index)=>({label:`${index===0?'0':price.tiers![index-1].max_input_tokens.toLocaleString()} ${index===0?'≤':'<'} 输入 ≤ ${tier.max_input_tokens.toLocaleString()}`,value:tier[side],index})):
      price.peak?[{label:'空闲时段',value:price[side],index:-1},{label:'高峰时段',value:price.peak[side],index:-2}]:[];
    return entries.length?<div className="tier-rates">{entries.map(entry=><label key={entry.index}>{entry.label}<InputNumber aria-label={`${model} ${entry.label} ${side==='input'?'输入':'输出'}价格`} min={0} precision={6} value={entry.value} disabled={loading||saving||!saved} onChange={value=>{if(value===null)return;setData(old=>{const next={...old.models[model]};if(next.note&&!next.note.startsWith('已手动修改'))next.note='已手动修改单价；'+next.note;if(entry.index>=0)next.tiers=next.tiers!.map((tier,index)=>index===entry.index?{...tier,[side]:value}:tier);else if(entry.index===-2)next.peak={...next.peak!,[side]:value};else next[side]=value;return {...old,models:{...old.models,[model]:next}}})}}/></label>)}</div>:rateInput(model,side,price[side]);
  };
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
        {title:'模型',dataIndex:'model',width:300,render:(value,row)=><div className="price-model"><code>{value}</code>{row.note&&<Typography.Text type="secondary">{row.note}</Typography.Text>}</div>},
        {title:'输入 · 元/百万 Token',render:(_,row)=>conditionalInput(row.model,'input',row)},
        {title:'输出 · 元/百万 Token',render:(_,row)=>conditionalInput(row.model,'output',row)},
        {title:'操作',width:70,render:(_,row)=><Button type="text" danger aria-label={`删除 ${row.model} 定价`} icon={<DeleteOutlined/>} disabled={saving} onClick={()=>setData(old=>{const models={...old.models};delete models[row.model];return {...old,models}})}/>},
      ]}/>
      <div className="table-footer">固定价格留空继承默认单价；分档价格按每次请求的输入长度选档，超出最大档位或旧数据缺少长度时标为未定价。<Typography.Link href="https://docs.volcengine.com/docs/ark/model-pricing?lang=zh" target="_blank" rel="noreferrer">官方价格来源</Typography.Link></div>
    </Card>
    <div className="save-bar"><div><b>{dirty?'有未保存的修改':'当前定价已保存'}</b><span>等效价格按已报告 Token 与当前单价估算。</span></div><Space><Button disabled={!dirty||saving} onClick={()=>{if(saved)setData(saved);setError('')}}>撤销修改</Button><Button type="primary" icon={<SaveOutlined/>} loading={saving} disabled={!dirty||loading} onClick={save}>保存定价</Button></Space></div>
  </div>;
}
