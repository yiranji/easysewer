import {qualifyBrowser} from './runner.js';
qualifyBrowser(status=>postMessage({status})).then(evidence=>postMessage({evidence}));
