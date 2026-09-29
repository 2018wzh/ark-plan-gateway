import {useRef, useState} from 'react';
import {Alert, Button, Collapse, Divider, Drawer, Form, Input, Modal, Select, Space, Typography} from 'antd';
import {api,errorText} from './api';
import type {Account} from './accounts';

type Values={label:string,plan:string,api_key?:string,models:string[],mapping?:string,quota_group?:string,access_key?:string,secret_key?:string};
export default function AccountEditor({account,accounts,onClose,onSaved}:{account:Account|null,accounts:Account[],onClose:()=>void,onSaved:()=>void}){
  const [form]=Form.useForm<Values>();
  const [saving,setSaving]=useState(false),[error,setError]=useState('');
  const submitting=useRef(false);
  const [modal,modalContext]=Modal.useModal();
  const close=()=>{if(saving)return;if(form.isFieldsTouched())modal.confirm({title:'放弃未保存的修改？',content:'关闭后，本次填写的内容不会保存。',okText:'放弃修改',cancelText:'继续编辑',onOk:onClose});else onClose()};
  const save=async(values:Values)=>{
    if(submitting.current)return;
    const access_key=values.access_key?.trim(),secret_key=values.secret_key?.trim();
    if(Boolean(access_key)!==Boolean(secret_key)){setError('更换查询凭据时，请同时填写 Access Key 和 Secret Key');return}
    const models=[...new Set(values.models.map(value=>value.trim()).filter(Boolean))];
    const mapping:Record<string,string>={};
    for(const line of (values.mapping||'').split('\n').map(value=>value.trim()).filter(Boolean)){
      const index=line.indexOf('=');
      const alias=line.slice(0,index).trim(),target=line.slice(index+1).trim();
      if(index<1||!target||!models.includes(alias)){setError('模型映射格式为「别名=上游模型」，别名必须包含在支持模型中');return}
      if(alias in mapping){setError(`模型别名重复：${alias}`);return}
      mapping[alias]=target;
    }
    submitting.current=true;setSaving(true);setError('');
    try{
      await api(account?`/accounts/${account.id}`:'/accounts',account?'PATCH':'POST',{
        label:values.label.trim(),...(!account?{plan:values.plan}:{}),models,model_mapping:mapping,
        ...(values.api_key?.trim()?{api_key:values.api_key.trim()}:{}),
        ...(access_key?{access_key,secret_key}:{}),...(account?{quota_group:values.quota_group}:{}),
      });
      onSaved();
    }catch(reason){setError(errorText(reason))}finally{setSaving(false);submitting.current=false}
  };
  return <Drawer title={account?'编辑账号':'添加账号'} open width="min(600px, 100vw)" onClose={close} maskClosable={!saving}
    footer={<div className="form-footer"><Typography.Text type="secondary">密钥加密保存，不会回显</Typography.Text><Space><Button onClick={close} disabled={saving}>取消</Button><Button type="primary" loading={saving} onClick={()=>form.submit()}>保存账号</Button></Space></div>}>
    {modalContext}
    <Form layout="vertical" form={form} disabled={saving} onFinish={save} initialValues={{label:account?.label,plan:account?.plan||'agent',models:account?.models||['ark-code-latest'],quota_group:account?.quota_group,mapping:Object.entries(account?.model_mapping||{}).map(([key,value])=>`${key}=${value}`).join('\n')}}>
      {error&&<Alert className="inline-alert" type="error" showIcon message={error}/>}
      <Form.Item name="label" label="账号名称" rules={[{required:true,whitespace:true,message:'请输入账号名称'},{max:100,message:'最多 100 个字符'}]}><Input placeholder="例如：主账号 Agent" autoComplete="off"/></Form.Item>
      <Form.Item name="plan" label="套餐类型"><Select disabled={!!account} options={[{value:'agent',label:'Agent Plan'},{value:'coding',label:'Coding Plan'}]}/></Form.Item>
      <Form.Item name="api_key" label="推理 API Key" extra={account?'已配置；留空保留原密钥':undefined} rules={[{required:!account,message:'请输入 API Key'},({getFieldValue})=>({validator(_,value){return !value||value.trim().length>=5?Promise.resolve():Promise.reject(new Error('API Key 至少 5 个字符'))}})]}><Input.Password autoComplete="new-password" placeholder={account?'留空不修改':'输入此账号的套餐 API Key'}/></Form.Item>
      <Form.Item name="models" label="支持模型" extra="输入模型名称后按回车，可添加多个模型" rules={[{required:true,type:'array',min:1,message:'至少配置一个模型'}]}><Select mode="tags" tokenSeparators={[',','，']} options={[...new Set(accounts.flatMap(a=>a.models))].map(value=>({value,label:value}))}/></Form.Item>
      <Divider orientation="left">额度查询</Divider>
      <Typography.Paragraph type="secondary">配置同一账号的 AK/SK 后可读取官方额度；不填写时仍可转发推理请求。</Typography.Paragraph>
      <Form.Item name="access_key" label="Access Key"><Input.Password autoComplete="new-password" placeholder={account?.has_ak_sk?'已配置，留空不修改':'选填，与 Secret Key 成对填写'}/></Form.Item>
      <Form.Item name="secret_key" label="Secret Key"><Input.Password autoComplete="new-password" placeholder={account?.has_ak_sk?'已配置，留空不修改':'选填，与 Access Key 成对填写'}/></Form.Item>
      <Collapse ghost items={[{key:'advanced',label:'高级配置',forceRender:true,children:<>
        <Form.Item name="mapping" label="模型映射" extra="每行填写：别名=上游模型；不填写则原样转发"><Input.TextArea rows={3} placeholder="ark-code-latest=上游模型名称"/></Form.Item>
        {account&&<Form.Item name="quota_group" label="共享额度主体" extra="只有同一套餐、同一实际账号的密钥才应共享额度"><Select options={accounts.filter(a=>a.plan===account.plan).map(a=>({value:a.id,label:a.id===account.id?`${a.label}（独立额度）`:a.label}))}/></Form.Item>}
      </>}]} />
    </Form>
  </Drawer>;
}
