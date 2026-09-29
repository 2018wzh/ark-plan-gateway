export class ApiError extends Error {
  constructor(public status:number, message:string){super(message)}
}

const errors:Record<string,string> = {
  'invalid password':'管理密码不正确', 'login required':'登录已过期，请重新登录',
  'account not found':'账号不存在，请刷新列表', 'API key already exists':'此 API Key 已存在，请编辑已有账号',
  'AK/SK must be provided together':'请同时填写 Access Key 和 Secret Key',
  'model mapping must use configured model names':'模型映射的别名必须在支持模型中',
};

export async function api(path:string,method='GET',body?:unknown){
  let response:Response;
  try{
    response=await fetch('/api'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  }catch{throw new Error('无法连接服务，请检查连接后重试')}
  if(!response.ok){
    const data=await response.json().catch(()=>({}));
    const detail=typeof data.detail==='string'?data.detail:response.status===422?'提交内容不符合要求，请检查表单':`请求失败（${response.status}）`;
    throw new ApiError(response.status,errors[detail]||detail);
  }
  return response.json();
}

export const errorText=(error:unknown)=>error instanceof Error?error.message:'操作失败，请重试';
