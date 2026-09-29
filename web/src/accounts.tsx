import {Badge} from 'antd';

export type Usage = {quota:number,used:number,reset_time:number|null,unit?:string};
export type Account = {id:string,plan:string,label:string,api_key_mask:string,has_ak_sk:boolean,quota_group:string,models:string[],model_mapping:Record<string,string>,enabled:number,auth_failed:number,expired:number,cooldown_until:number|null,cooldown_kind:string|null,quota_checked_at:number|null,quota_error:string|null,usage:Record<string,Usage>,inflight?:number};
export const windowName=(name:string)=>({AFPFiveHour:'5 小时',AFPDaily:'每日',AFPWeekly:'每周',AFPMonthly:'每月',session:'5 小时',weekly:'每周',monthly:'每月'}[name]||name);
export const formatTime=(time:number|null|undefined)=>time&&time>0?new Date(time*1000).toLocaleString('zh-CN',{hour12:false}):'重置时间未知';
export const remaining=(usage:Usage)=>usage.quota>0?Math.max(0,Math.min(100,100-usage.used/usage.quota*100)):null;
export const quotaColor=(value:number)=>value<=15?'#d76542':value<=40?'#c28a26':'#158f98';
export function accountStatus(account:Account,now=Date.now()/1000){
  if(!account.enabled)return {key:'disabled',label:'已停用',color:'#91a0a6'};
  if(account.auth_failed)return {key:'error',label:'鉴权失败',color:'#c94343'};
  if(account.expired)return {key:'error',label:'套餐过期',color:'#c94343'};
  if(account.cooldown_kind&&(account.cooldown_until===null||account.cooldown_until>now))return {key:'cooling',label:account.cooldown_kind==='rate'?'限流退避':'冷却',color:'#d76542'};
  return {key:'active',label:'活跃',color:'#16a085'};
}
export function StatusBadge({account}:{account:Account}){const status=accountStatus(account);return <Badge color={status.color} text={status.label}/>}
