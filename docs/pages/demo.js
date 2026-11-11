const demo = document.querySelector('#demo');
const demoPage = document.querySelector('#demo-page');
const address = document.querySelector('#demo-address');
const owner = document.querySelector('#demo-owner');
const title = document.querySelector('#demo-page-title');
const form = document.querySelector('#demo-form');
const nameInput = document.querySelector('#demo-name');
const submit = document.querySelector('#demo-submit');
const receipt = document.querySelector('#demo-receipt');
const status = document.querySelector('#demo-status');
const action = document.querySelector('#demo-action');
const stepList = document.querySelector('#demo-steps');
const stepButtons = [...stepList.querySelectorAll('button')];
const traceEvents = [...demo.querySelectorAll('[data-event]')];
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const sampleName = 'Ada Lovelace';

const phases = [
  { owner: 'Agent', status: 'Take control to fill the sample form.', action: 'Take control' },
  { owner: 'You', status: 'Your turn. Submit the sample reservation.', action: 'Return control' },
  { owner: 'Agent', status: 'Agent verified the confirmation on the same page.', action: 'Replay demo' },
];

let currentStep = 0;
let reservationName = sampleName;
let pageAnimation;

function showStep(step, confirmed = step === 2) {
  currentStep = step;
  const phase = phases[step];
  const manual = step === 1;
  const editing = manual && !confirmed;

  demo.dataset.phase = manual ? 'human' : 'agent';
  owner.textContent = phase.owner;
  address.textContent = `127.0.0.1:8001/${confirmed ? 'received' : 'reservation'}`;
  title.textContent = confirmed ? 'Reservation received' : 'Reservation details';
  form.hidden = confirmed;
  nameInput.disabled = !editing;
  nameInput.value = editing ? sampleName : '';
  submit.hidden = !editing;
  receipt.hidden = !confirmed;
  receipt.textContent = confirmed ? `A sample spot was reserved for ${reservationName}.` : '';
  status.textContent = manual && confirmed ? 'Reservation submitted. Hand the page back to the agent.' : phase.status;
  action.firstChild.nodeValue = `${phase.action} `;
  action.hidden = editing;

  stepButtons.forEach((button, index) => button.setAttribute('aria-pressed', String(index === step)));
  traceEvents.forEach((event, index) => {
    event.classList.toggle('current', index === step);
    event.classList.toggle('done', index < step);
    if (index === step) event.setAttribute('aria-current', 'step');
    else event.removeAttribute('aria-current');
  });

  pageAnimation?.cancel();
  if (!reducedMotion.matches) {
    pageAnimation = demoPage.animate(
      [{ opacity: .6, transform: 'translateY(3px)' }, { opacity: 1, transform: 'translateY(0)' }],
      { duration: 230, easing: 'ease-out' },
    );
  }

  if (editing) {
    nameInput.focus();
    nameInput.select();
  }
}

stepButtons.forEach((button, index) => {
  button.addEventListener('click', () => {
    reservationName = sampleName;
    showStep(index);
  });
});

action.addEventListener('click', () => {
  if (currentStep === 0) {
    showStep(1);
  } else if (currentStep === 1) {
    showStep(2);
  } else {
    reservationName = sampleName;
    showStep(0);
  }
});

form.addEventListener('submit', (event) => {
  event.preventDefault();
  reservationName = nameInput.value.trim() || sampleName;
  showStep(1, true);
  action.focus();
});

stepList.hidden = false;
action.hidden = false;
traceEvents[0].setAttribute('aria-current', 'step');

const copy = document.querySelector('#copy-setup');
const commands = document.querySelector('#setup-commands');
let copyFeedback;
copy.hidden = false;
copy.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(commands.textContent);
    copy.textContent = 'Copied';
  } catch {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(commands);
    selection.removeAllRanges();
    selection.addRange(range);
    copy.textContent = 'Selected. Press Ctrl+C or ⌘C';
  }
  clearTimeout(copyFeedback);
  copyFeedback = setTimeout(() => { copy.textContent = 'Copy commands'; }, 2400);
});

const replay = document.querySelector('#replay');
const replayImage = document.querySelector('#replay-image');
const replayToggle = document.querySelector('#toggle-replay');
const poster = replayImage.getAttribute('src');
let replayTimer;
let playing = false;

function stopReplay() {
  clearTimeout(replayTimer);
  playing = false;
  replayImage.src = poster;
  replayToggle.textContent = 'Play GIF';
}

replayToggle.hidden = false;
replayToggle.addEventListener('click', () => {
  if (playing) {
    stopReplay();
    return;
  }
  playing = true;
  replayImage.src = 'assets/handoff.gif';
  replayToggle.textContent = 'Stop GIF';
});
replayImage.addEventListener('load', () => {
  // The asset plays once and runs for 8.56 seconds.
  if (playing) replayTimer = setTimeout(stopReplay, 8800);
});
replayImage.addEventListener('error', () => {
  if (playing) stopReplay();
});
replay.addEventListener('toggle', () => {
  if (!replay.open && playing) stopReplay();
});
