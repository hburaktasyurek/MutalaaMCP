const copyButton = document.getElementById('copy-setup');
const promptField = document.getElementById('setup-prompt');
const copyStatus = document.getElementById('copy-status');
if (copyButton && promptField && copyStatus) {
  copyButton.hidden = false;
  copyButton.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(promptField.value);
      copyStatus.textContent = copyButton.dataset.success;
    } catch {
      promptField.focus();
      promptField.select();
      copyStatus.textContent = copyButton.dataset.failure;
    }
  });
}
