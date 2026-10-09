/**
 * 实时数据：30 秒轮询（E12）+ 手动刷新（E13）
 *
 * 两条 legacy 经验（都在这里兑现）
 * =================================
 *
 * 1. **失败不要清空数据**：网络抖一下就把页面变空，用户会以为服务挂了。
 *    轮询失败只累加计数器 + 显示一个「数据可能不是最新」的提示，
 *    上一次成功的数据继续展示。
 * 2. **页面不可见时不轮询**：手机锁屏 / 切到别的标签页时继续每 30 秒打
 *    一次接口纯属浪费（旧版就是这样）。`visibilitychange` 恢复时**立刻补一次**，
 *    这样回到页面马上看到新数据。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { LivePayload } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

//: 轮询间隔（E12：legacy 的 30 秒）
export const POLL_MS = 30_000

export function useLive() {
  const auth = useAuthStore()
  const payload = ref<LivePayload | null>(null)
  const loading = ref(true)
  const refreshing = ref(false)
  const error = ref('')
  /** 连续失败次数（>0 时界面提示「数据可能不是最新」） */
  const failures = ref(0)
  const refreshError = ref('')
  const refreshNotice = ref('')

  let timer: number | undefined

  async function load(): Promise<void> {
    try {
      payload.value = await api.live()
      failures.value = 0
      error.value = ''
    } catch (err) {
      if (auth.handle(err)) return // 401/403 由守卫接管
      failures.value += 1
      // ⚠️ 不清空 payload：旧数据比空白有用
      error.value = err instanceof Error ? err.message : '加载失败'
    } finally {
      loading.value = false
    }
  }

  /** E13：手动刷新（服务端抓取，不推送飞书） */
  async function refresh(): Promise<void> {
    if (refreshing.value) return
    refreshing.value = true
    refreshError.value = ''
    refreshNotice.value = ''
    try {
      const result = await api.refresh()
      if (result.ok) {
        refreshNotice.value = `已刷新（${result.records} 条记录）`
        await load()
      } else {
        refreshError.value = result.error ?? '刷新失败'
      }
    } catch (err) {
      if (auth.handle(err)) return
      refreshError.value = err instanceof Error ? err.message : '刷新失败'
    } finally {
      refreshing.value = false
    }
  }

  function start(): void {
    stop()
    timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, POLL_MS)
  }

  function stop(): void {
    if (timer !== undefined) {
      window.clearInterval(timer)
      timer = undefined
    }
  }

  function onVisible(): void {
    if (document.visibilityState === 'visible') void load()
  }

  onMounted(() => {
    void load()
    start()
    document.addEventListener('visibilitychange', onVisible)
  })

  onBeforeUnmount(() => {
    stop()
    document.removeEventListener('visibilitychange', onVisible)
  })

  return {
    payload,
    loading,
    refreshing,
    error,
    failures,
    refreshError,
    refreshNotice,
    load,
    refresh,
  }
}
