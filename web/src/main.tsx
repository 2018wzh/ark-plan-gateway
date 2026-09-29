import {lazy,Suspense,useCallback,useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Alert,Button,Card,ConfigProvider,Drawer,Form,Grid,Input,Layout,Menu,Modal,Space,Spin,Table,Typography} from 'antd';
import {ApiOutlined,BarChartOutlined,DatabaseOutlined,DollarOutlined,LogoutOutlined,MenuOutlined,PlusOutlined,ReloadOutlined,SafetyCertificateOutlined,SettingOutlined,TeamOutlined} from '@ant-design/icons';
import zhCN from 'antd/locale/zh_CN';
import {api,ApiError,errorText} from './api';
import {Account,StatusBadge} from './accounts';
import AccountsPanel from './AccountsPanel';
import AccountEditor from './AccountEditor';
import Pricing from './Pricing';
import SettingsPanel from './SettingsPanel';
import './style.css';

const Statistics=lazy(()=>import('./Statistics'));
const pages=[
  {key:'accounts',label:'账号与额度',description:'管理套餐密钥、可用额度与账号状态',icon:<TeamOutlined/>},
  {key:'statistics',label:'额度与统计',description:'按模型查看 Token 消耗与等效价格',icon:<BarChartOutlined/>},
  {key:'pricing',label:'模型定价',description:'配置默认单价与各模型的专属价格',icon:<DollarOutlined/>},
  {key:'routes',label:'路由状态',description:'查看模型映射、账号状态与当前并发',icon:<ApiOutlined/>},
  {key:'settings',label:'服务设置',description:'管理额度刷新与访问凭据',icon:<SettingOutlined/>},
];
function Brand(){return <div className="brand"><DatabaseOutlined/><div><strong>Ark</strong><span>Plan Gateway</span></div></div>}

function App(){
  const [logged,setLogged]=useState<boolean|null>(null),[loginBusy,setLoginBusy]=useState(false),[loginError,setLoginError]=useState('');
  const [tab,setTab]=useState(()=>pages.some(p=>p.key===location.hash.slice(1))?location.hash.slice(1):'accounts');
  const [accounts,setAccounts]=useState<Account[]>([]),[updatedAt,setUpdatedAt]=useState<number|null>(null),[loadError,setLoadError]=useState(''),[refreshing,setRefreshing]=useState(false);
  const [editor,setEditor]=useState<Account|null|undefined>(),[mobileOpen,setMobileOpen]=useState(false),[dirty,setDirty]=useState(false);
  const [modal,modalContext]=Modal.useModal();
  const screens=Grid.useBreakpoint();
  const loadingRequest=useRef<Promise<void>|null>(null);
  const generation=useRef(0);
  const load=useCallback(async()=>{
    if(loadingRequest.current)return loadingRequest.current;
    const current=generation.current;
    setRefreshing(true);
    const request=(async()=>{try{
      const result=await api('/routes');
      if(current!==generation.current)return;
      setAccounts(result);setUpdatedAt(Date.now());setLoadError('');setLogged(true);
    }catch(error){if(current!==generation.current)return;if(error instanceof ApiError&&error.status===401){setLogged(false);setLoadError('')}else setLoadError(errorText(error))}
    finally{setRefreshing(false);loadingRequest.current=null}})();
    loadingRequest.current=request;
    return request;
  },[]);
  useEffect(()=>{void load()},[load]);
  useEffect(()=>{if(!logged)return;const timer=setInterval(()=>{void load()},15000);return()=>clearInterval(timer)},[logged,load]);
  useEffect(()=>{if(!dirty)return;const warn=(event:BeforeUnloadEvent)=>{event.preventDefault();event.returnValue=''};window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn)},[dirty]);
  const guard=(next:()=>void)=>{if(dirty)modal.confirm({title:'有未保存的修改',content:'离开后，本页尚未保存的修改将丢失。',okText:'放弃修改并离开',cancelText:'继续编辑',onOk:()=>{setDirty(false);next()}});else next()};
  const navigate=(key:string)=>{if(key===tab){setMobileOpen(false);return}guard(()=>{setTab(key);history.replaceState(null,'',`#${key}`);setMobileOpen(false)})};
  const logout=()=>guard(async()=>{try{await api('/logout','POST');generation.current++;setDirty(false);setEditor(undefined);setLogged(false);setLoginError('')}catch(error){setLoadError(errorText(error))}});
  const page=pages.find(p=>p.key===tab)!;
  const nav=<div className="nav-inner"><Brand/><Menu theme="dark" mode="inline" selectedKeys={[tab]} items={pages.map(({key,label,icon})=>({key,label,icon}))} onClick={({key})=>navigate(key)}/><div className="nav-footer"><span><SafetyCertificateOutlined/> 管理控制台</span><Button type="text" icon={<LogoutOutlined/>} onClick={logout}>退出登录</Button></div></div>;

  if(logged===null)return <div className="startup"><Spin size="large"/><Typography.Text>正在连接管理控制台</Typography.Text>{loadError&&<Alert type="error" message={loadError} action={<Button onClick={load}>重试</Button>}/>}</div>;
  if(!logged)return <div className="login"><div className="login-shell"><div className="login-brand"><Brand/><h1>账号与额度，<br/>一处管理。</h1><p>Agent Plan / Coding Plan</p><span>Responses / Chat 网关管理控制台</span></div><div className="login-form"><Typography.Title level={2}>登录控制台</Typography.Title><Typography.Paragraph type="secondary">使用管理密码访问账号池与用量统计。</Typography.Paragraph>{loginError&&<Alert className="inline-alert" showIcon type="error" message={loginError}/>}<Form layout="vertical" onFinish={async(values:{password:string})=>{if(loginBusy)return;setLoginBusy(true);setLoginError('');try{await api('/login','POST',values);await load()}catch(error){setLoginError(errorText(error))}finally{setLoginBusy(false)}}}><Form.Item name="password" label="管理密码" rules={[{required:true,message:'请输入管理密码'}]}><Input.Password autoComplete="current-password" size="large" placeholder="请输入管理密码"/></Form.Item><Button type="primary" htmlType="submit" size="large" block loading={loginBusy}>登录</Button></Form><div className="login-footnote"><SafetyCertificateOutlined/> 管理密码与下游访问令牌相互独立</div></div></div></div>;
  return <Layout className="shell">{modalContext}
    {screens.lg?<Layout.Sider className="desktop-nav" width={224}>{nav}</Layout.Sider>:<Drawer className="mobile-nav" placement="left" width={264} open={mobileOpen} onClose={()=>setMobileOpen(false)} closable title="导航" styles={{body:{padding:0}}}>{nav}</Drawer>}
    <Layout.Content className="content">
      <header className="top"><div className="page-title">{!screens.lg&&<Button className="nav-toggle" aria-label="打开导航" icon={<MenuOutlined/>} onClick={()=>setMobileOpen(true)}/>}<div><Typography.Title level={2}>{page.label}</Typography.Title><Typography.Text type="secondary">{page.description}</Typography.Text></div></div><Space className="page-actions">
        {(tab==='accounts'||tab==='routes')&&<Button icon={<ReloadOutlined/>} loading={refreshing} onClick={load}>刷新列表</Button>}
        {tab==='accounts'&&<Button type="primary" icon={<PlusOutlined/>} onClick={()=>setEditor(null)}>添加账号</Button>}
      </Space></header>
      {loadError&&<Alert className="inline-alert" type="warning" showIcon message="自动更新暂不可用，当前显示上次数据" description={loadError} action={<Button size="small" onClick={load}>重试</Button>}/>}
      {tab==='accounts'&&<AccountsPanel accounts={accounts} onEdit={setEditor} reload={load} updatedAt={updatedAt}/>}
      {tab==='statistics'&&<Suspense fallback={<Card loading/>}><Statistics accounts={accounts} api={api}/></Suspense>}
      {tab==='pricing'&&<Pricing models={[...new Set(accounts.flatMap(a=>a.models))]} onDirty={setDirty}/>}
      {tab==='settings'&&<SettingsPanel onDirty={setDirty} onPasswordChanged={()=>{generation.current++;setDirty(false);setLogged(false)}}/>}
      {tab==='routes'&&<Card title="模型与账号"><Table<Account> rowKey="id" dataSource={accounts} pagination={false} scroll={{x:900}} columns={[
        {title:'账号',dataIndex:'label',render:(_,a)=><Button type="link" onClick={()=>setEditor(a)}>{a.label}</Button>},
        {title:'支持模型',dataIndex:'models',render:(models:string[])=>models.join('、')},
        {title:'模型映射',dataIndex:'model_mapping',render:(value:Record<string,string>)=>Object.entries(value).map(([key,value])=>`${key} → ${value}`).join('、')||'原样转发'},
        {title:'状态',render:(_,a)=><StatusBadge account={a}/>},
        {title:'当前并发',dataIndex:'inflight'},
        {title:'共享额度主体',dataIndex:'quota_group',render:(id:string)=>accounts.find(a=>a.id===id)?.label||'未找到'},
      ]}/></Card>}
      <footer className="app-footer">Ark Plan Gateway <span>Agent Plan / Coding Plan</span></footer>
    </Layout.Content>
    {editor!==undefined&&<AccountEditor account={editor} accounts={accounts} onClose={()=>setEditor(undefined)} onSaved={()=>{setEditor(undefined);void load()}}/>}
  </Layout>;
}

createRoot(document.getElementById('root')!).render(<ConfigProvider locale={zhCN} theme={{token:{colorPrimary:'#158f98',colorInfo:'#158f98',colorSuccess:'#16a085',colorWarning:'#c28a26',colorText:'#18343d',colorTextSecondary:'#6d8088',colorBorder:'#dce5e9',colorBgLayout:'#f4f7f8',borderRadius:10,fontFamily:'Inter, "Segoe UI", "Microsoft YaHei", sans-serif',fontSize:14,controlHeight:38},components:{Menu:{darkItemBg:'#142c35',darkSubMenuItemBg:'#142c35',darkItemSelectedBg:'#215562',darkItemSelectedColor:'#fff',itemHeight:48},Table:{headerBg:'#f6f9fa',headerColor:'#637780',rowHoverBg:'#f5fbfb'},Card:{headerFontSize:16},Button:{primaryShadow:'none'}}}}><App/></ConfigProvider>);
