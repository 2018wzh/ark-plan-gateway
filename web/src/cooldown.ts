type CoolingAccount={enabled:number,auth_failed:number,expired:number,cooldown_kind:string|null,cooldown_until:number|null};

export function poolCooldown(accounts:CoolingAccount[],now:number){
  const cooling=accounts.filter(a=>a.enabled&&!a.auth_failed&&!a.expired&&a.cooldown_kind&&(a.cooldown_until===null||a.cooldown_until>now));
  const known=cooling.flatMap(a=>a.cooldown_until!==null&&Number.isFinite(a.cooldown_until)?[a.cooldown_until]:[]);
  return {count:cooling.length,until:known.length?Math.min(...known):null,exact:known.length===cooling.length};
}

export function formatWait(until:number,now:number){
  const seconds=Math.max(0,Math.ceil(until-now));
  const days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600),minutes=Math.floor(seconds%3600/60);
  return `${days?`${days}天 `:''}${days||hours?`${hours}小时 `:''}${days||hours||minutes?`${minutes}分 `:''}${seconds%60}秒`;
}
