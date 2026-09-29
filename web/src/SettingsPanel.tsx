import {useEffect,useState} from 'react';
import {Alert,Button,Card,Form,Input,InputNumber,Space,Typography,message} from 'antd';
import {SaveOutlined} from '@ant-design/icons';
import {api,errorText} from './api';

type Values={refresh_seconds:number,new_password?:string,new_service_token?:string};
export default function SettingsPanel({onDirty,onPasswordChanged}:{onDirty:(value:boolean)=>void,onPasswordChanged:()=>void}){
  const [form]=Form.useForm<Values>();
  const [saved,setSaved]=useState<Values>(),[loading,setLoading]=useState(true),[saving,setSaving]=useState(false),[dirty,setDirty]=useState(false),[error,setError]=useState('');
  const [msg,context]=message.useMessage();
  useEffect(()=>{onDirty(dirty);return()=>onDirty(false)},[dirty,onDirty]);
  const load=async()=>{setLoading(true);try{const result=await api('/settings');const value={refresh_seconds:result.refresh_seconds};setSaved(value);form.setFieldsValue(value);setError('')}catch(e){setError(errorText(e))}finally{setLoading(false)}};
  useEffect(()=>{void load()},[]);
  const save=async(values:Values)=>{if(saving)return;setSaving(true);setError('');try{
    await api('/settings','PATCH',{refresh_seconds:values.refresh_seconds,...(values.new_password?{new_password:values.new_password}:{}),...(values.new_service_token?{new_service_token:values.new_service_token}:{})});
    setSaved({refresh_seconds:values.refresh_seconds});setDirty(false);form.setFieldsValue({new_password:undefined,new_service_token:undefined});
    msg.success(values.new_password?'管理密码已更新，请重新登录':'设置已保存');
    if(values.new_password)onPasswordChanged();
  }catch(e){setError(errorText(e))}finally{setSaving(false)}};
  return <div className="settings-layout">{context}<Form form={form} layout="vertical" onFinish={save} disabled={loading||saving||!saved} onValuesChange={()=>setDirty(true)} className="page-stack">
    {error&&<Alert type="error" showIcon message={error} action={!saved?<Button onClick={load}>重试</Button>:undefined}/>}
    <Card title="额度刷新" loading={loading}><Form.Item name="refresh_seconds" label="自动刷新间隔" extra="30–3600 秒；查询配置了 AK/SK 的账号" rules={[{required:true,message:'请输入刷新间隔'},{type:'number',min:30,max:3600,message:'请设置 30–3600 秒'}]}><InputNumber min={30} max={3600} precision={0} addonAfter="秒" style={{width:220}}/></Form.Item></Card>
    <Card title="访问凭据"><Form.Item name="new_password" label="管理密码" extra="留空不修改；修改后当前会话需重新登录" rules={[{min:12,message:'管理密码至少 12 位'}]}><Input.Password autoComplete="new-password" placeholder="输入新密码，至少 12 位"/></Form.Item>
      <Form.Item name="new_service_token" label="下游访问令牌" extra="留空不修改；修改后需同步更新下游客户端配置" rules={[{min:24,message:'访问令牌至少 24 位'}]}><Input.Password autoComplete="new-password" placeholder="输入新令牌，至少 24 位"/></Form.Item></Card>
    <div className="save-bar"><Typography.Text type="secondary">{dirty?'有未保存的修改':'当前设置已保存'}</Typography.Text><Space><Button disabled={!dirty||saving} onClick={()=>{form.resetFields();form.setFieldsValue(saved||{});setDirty(false);setError('')}}>撤销修改</Button><Button type="primary" icon={<SaveOutlined/>} htmlType="submit" disabled={!dirty||loading} loading={saving}>保存设置</Button></Space></div>
  </Form><Card title="接入信息" className="connection-card"><Typography.Paragraph type="secondary">客户端使用下游访问令牌，管理密码仅用于此控制台。</Typography.Paragraph><div className="connection-field"><span>网关地址</span><Typography.Text copyable>{window.location.origin}</Typography.Text></div><div className="connection-field"><span>Responses 接口</span><Typography.Text copyable>{window.location.origin}/v1/responses</Typography.Text></div></Card></div>;
}
