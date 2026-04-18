# Fujifilm SDK notes

Ce projet utilise uniquement le backend Fujifilm SDK.

Points importants :
- placer le SDK dans `./sdk` ou renseigner `camera.sdk.sdk_root`
- sur macOS, vérifier la présence de `FTLPTP.dylib`, `FTLPTPIP.dylib` et `XAPI.bundle`
- lancer l'application avec le boîtier déjà connecté si possible
- configurer le boîtier dans un mode compatible avec le déclenchement distant
