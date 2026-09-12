const copyButton = document.getElementById('copy-setup');
const promptField = document.getElementById('setup-prompt');
const copyStatus = document.getElementById('copy-status');
if (copyButton && promptField && copyStatus) {
  copyButton.hidden = false;
  const labelIdle = copyButton.textContent;
  let revertTimer;
  copyButton.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(promptField.value);
      copyStatus.textContent = copyButton.dataset.success;
      if (copyButton.dataset.labelCopied) {
        copyButton.textContent = copyButton.dataset.labelCopied;
        clearTimeout(revertTimer);
        revertTimer = setTimeout(() => {
          copyButton.textContent = labelIdle;
        }, 2200);
      }
    } catch {
      promptField.focus();
      promptField.select();
      copyStatus.textContent = copyButton.dataset.failure;
    }
  });
}
