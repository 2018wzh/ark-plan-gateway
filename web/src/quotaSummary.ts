import type {Account,Usage} from './accounts';

const windows = [
  {key:'fiveHour',label:'5 小时',names:['AFPFiveHour','session']},
  {key:'daily',label:'每日',names:['AFPDaily','daily']},
  {key:'weekly',label:'每周',names:['AFPWeekly','weekly']},
  {key:'monthly',label:'每月',names:['AFPMonthly','monthly']},
];
export function quotaSummary(accounts:Account[]) {
  const groups = new Map<string,Account[]>();
  for(const account of accounts.filter(a=>a.enabled&&!a.expired))
    groups.set(account.quota_group,[...(groups.get(account.quota_group)||[]),account]);
  return windows.map(window=>{
    const entries:{account:Account,usage:Usage}[]=[];
    let expected=0;
    for(const members of groups.values()) {
      if(window.key==='daily'&&!members.some(a=>a.plan==='agent'||window.names.some(n=>n in a.usage)))continue;
      expected++;
      const candidates=members.flatMap(account=>window.names.filter(n=>account.usage[n]).map(n=>({account,usage:account.usage[n]})))
        .filter(({usage:u})=>Number.isFinite(u.quota)&&u.quota>0&&Number.isFinite(u.used)&&u.used>=0)
        .sort((a,b)=>(b.account.quota_checked_at||0)-(a.account.quota_checked_at||0));
      if(candidates[0])entries.push(candidates[0]);
    }
    const ratios=entries.map(({usage:u})=>Math.max(0,Math.min(100,100-u.used/u.quota*100)));
    const afp=entries.filter(({account})=>account.plan==='agent');
    const resets=entries.map(({usage})=>usage.reset_time).filter((v):v is number=>v!==null&&Number.isFinite(v)&&v>0);
    return {...window,known:entries.length,unknown:expected-entries.length,
      percent:ratios.length?ratios.reduce((sum,r)=>sum+r,0)/ratios.length:null,
      exhausted:ratios.filter(r=>r===0).length,
      stale:entries.filter(({account})=>!!account.quota_error).length,
      afpRemaining:afp.reduce((sum,{usage:u})=>sum+Math.max(0,u.quota-u.used),0),
      afpTotal:afp.reduce((sum,{usage:u})=>sum+u.quota,0),
      nextReset:resets.length?Math.min(...resets):null,
      checkedAt:entries.length&&entries.every(({account})=>account.quota_checked_at)?Math.min(...entries.map(({account})=>account.quota_checked_at!)):null};
  });
}
