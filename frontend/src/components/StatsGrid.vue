<template>
  <div class="stats">
    <div class="stat-card">
      <div class="value">{{ status.active_accounts }}</div><div class="label">活跃账号</div>
    </div>
    <div class="stat-card">
      <div class="value">{{ status.active_keys }}</div><div class="label">活跃 Key</div>
    </div>
    <div class="stat-card">
      <div class="value">{{ status.active_tenants }}</div><div class="label">租户</div>
    </div>
    <div class="stat-card">
      <div class="value">{{ status.unhealthy_accounts }}</div><div class="label">异常账号</div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  status: { type: Object, default: () => ({}) }
})
</script>
