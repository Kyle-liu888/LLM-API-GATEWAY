<template>
  <div class="modal-overlay" :class="{ show: visible }" @click.self="$emit('close')">
    <div class="modal">
      <h3>{{ title }}</h3>
      <slot></slot>
      <div class="modal-actions" v-if="$slots.actions"><slot name="actions"></slot></div>
      <slot name="extra"></slot>
    </div>
  </div>
</template>

<script setup>
defineProps({ visible: Boolean, title: String })
defineEmits(['close'])
</script>
