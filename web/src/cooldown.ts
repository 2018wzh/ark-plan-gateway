type CoolingAccount={enabled:number,auth_failed:number,expired:number,cooldown_kind:string|null,cooldown_until:number|null,models?:string[],model_mapping?:Record<string,string>,model_blocks?:{model:string,retry_at:number|null}[]};

export function effectiveCooldown(account:CoolingAccount,now:number){
  const base=account.cooldown_kind&&(account.cooldown_until===null||account.cooldown_until>now)?{kind:account.cooldown_kind,until:account.cooldown_until,exact:true}:null;
  if(base?.kind==='account'||base&&base.until===null)return base;
  const models=account.models||[];
  const blocked=models.map(m=>account.model_blocks?.find(b=>b.model===(account.model_mapping?.[m]||m)&&(b.retry_at===null||b.retry_at>now)));
  if(!models.length||blocked.some(b=>!b))return base;
  const known=blocked.flatMap(b=>b?.retry_at!==null&&b?.retry_at!==undefined?[b.retry_at]:[]);
  if(!known.length)return {kind:'account',until:null,exact:false};
  return {kind:base?.kind||'model',until:Math.max(base?.until||0,Math.min(...known)),exact:known.length===blocked.length};
}

export function poolCooldown(accounts:CoolingAccount[],now:number){
  const cooling=accounts.filter(a=>a.enabled&&!a.auth_failed&&!a.expired).map(a=>effectiveCooldown(a,now)).filter(a=>a&&a.kind!=='account');
  const known=cooling.flatMap(a=>a?.until!==null&&a?.until!==undefined&&Number.isFinite(a.until)?[a.until]:[]);
  return {count:cooling.length,until:known.length?Math.min(...known):null,exact:known.length===cooling.length&&cooling.every(a=>a?.exact)};
}

export function formatWait(until:number,now:number){
  const seconds=Math.max(0,Math.ceil(until-now));
  const days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600),minutes=Math.floor(seconds%3600/60);
  return `${days?`${days}天 `:''}${days||hours?`${hours}小时 `:''}${days||hours||minutes?`${minutes}分 `:''}${seconds%60}秒`;
}
