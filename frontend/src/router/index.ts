/**
 * 路由 + 两条守卫（M5 §5.3）
 *
 * 守卫顺序（B4/Q19 的关键）
 * =========================
 *
 * ::
 *
 *     未登录            → /login
 *     已登录但未改密     → /password（**除改密页外哪里都去不了**）
 *     已登录且改过密     → 正常放行
 *
 * 为什么必须把改密排在「正常放行」之前：后端对 `must_change_password=1` 的
 * 会话会 403 掉除 `/api/auth/{password,logout,me}` 外的**所有**接口
 * （见 `starwatt/auth/decorators.py`）。前端若放行到 dashboard，用户会看到
 * 满屏「请求失败」，而真正该做的是先改密。
 *
 * 页面清单见 `REWRITE_PLAN` §M5（11 个页面）—— 本文件先落地骨架与
 * 认证链路的 3 个页面，其余按里程碑逐步补齐。
 */
import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'dashboard',
      component: () => import('@/views/DashboardView.vue'),
      meta: { title: '总览' },
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/password',
      name: 'password',
      component: () => import('@/views/PasswordView.vue'),
      meta: { title: '修改密码' },
    },
    {
      path: '/admin',
      component: () => import('@/views/admin/AdminShell.vue'),
      meta: { title: '管理', admin: true },
      children: [
        { path: '', redirect: { name: 'admin-config' } },
        {
          path: 'config',
          name: 'admin-config',
          component: () => import('@/views/admin/AdminConfigView.vue'),
          meta: { title: '配置中心', admin: true },
        },
        {
          path: 'users',
          name: 'admin-users',
          component: () => import('@/views/admin/AdminUsersView.vue'),
          meta: { title: '用户', admin: true },
        },
        {
          path: 'test',
          name: 'admin-test',
          component: () => import('@/views/admin/AdminTestView.vue'),
          meta: { title: '连通测试', admin: true },
        },
        {
          path: 'audit',
          name: 'admin-audit',
          component: () => import('@/views/admin/AdminAuditView.vue'),
          meta: { title: '审计日志', admin: true },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFoundView.vue'),
      meta: { public: true, title: '页面不存在' },
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 首次进入（或刷新）先探身份 —— 只做一次，后续走内存状态
  if (!auth.ready) await auth.bootstrap()

  if (to.meta.public) {
    // 已登录还去登录页 → 回首页（否则改完密点后退会回到登录页，很怪）
    if (to.name === 'login' && auth.authenticated) return { name: 'dashboard' }
    return true
  }

  if (!auth.authenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  if (auth.mustChangePassword && to.name !== 'password') {
    return { name: 'password' }
  }

  // 管理分支：前端只隐藏入口（体验），后端 admin_access 才是安全边界
  if (to.meta.admin && !auth.isAdmin) {
    return { name: 'dashboard' }
  }

  // 改完密就不该再停留在改密页
  if (!auth.mustChangePassword && to.name === 'password' && to.query.forced !== '1') {
    return { name: 'dashboard' }
  }

  return true
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · StarWatt 星瓦` : 'StarWatt 星瓦'
})

export default router
