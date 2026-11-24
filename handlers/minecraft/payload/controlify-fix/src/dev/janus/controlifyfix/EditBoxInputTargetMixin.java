package dev.janus.controlifyfix;

import net.minecraft.client.gui.components.EditBox;
import net.minecraft.client.input.KeyEvent;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Minecraft 26's Gui.setScreen clears the focus of the screen it leaves, so when
 * Controlify opens its on-screen keyboard (a screen of its own) the edit box it
 * types into loses focus, and EditBox.charTyped/keyPressed then ignore every key.
 * Give the box its focus back before Controlify sends it anything.
 *
 * Its special keys (Del = backspace, cursor keys) also arrive without the SDL
 * keycode EditBox.keyPressed switches on: fill it in.
 */
@Mixin(targets = "dev.isxander.controlify.screenop.compat.vanilla.EditBoxComponentProcessor$EditBoxInputTarget",
       remap = false)
public abstract class EditBoxInputTargetMixin {
    @Shadow @Final private EditBox editBox;

    private void janus$refocus() {
        if (!this.editBox.isFocused()) {
            this.editBox.setFocused(true);
        }
    }

    @Inject(method = "acceptChar", at = @At("HEAD"), remap = false)
    private void janus$beforeChar(char ch, int modifiers, CallbackInfoReturnable<Boolean> cir) {
        janus$refocus();
    }

    @Inject(method = "acceptKeyCode", at = @At("HEAD"), remap = false)
    private void janus$beforeKey(int keycode, int scancode, int modifiers, CallbackInfoReturnable<Boolean> cir) {
        janus$refocus();
    }

    /** Minecraft 26's KeyEvent is (key = SDL scancode, keycode = SDL keycode,
     *  modifiers) and EditBox.keyPressed checks the keycode; Controlify passes
     *  the scancode (InputConstants.KEY_*) and 0 as the keycode. */
    private static int janus$sdlKeycode(int scancode) {
        switch (scancode) {
            case 40: return 13;           // RETURN
            case 41: return 27;           // ESCAPE
            case 42: return 8;            // BACKSPACE
            case 43: return 9;            // TAB
            case 76: return 127;          // DELETE
            case 74: case 75: case 77: case 78:          // HOME, PAGEUP, END, PAGEDOWN
            case 79: case 80: case 81: case 82:          // RIGHT, LEFT, DOWN, UP
                return scancode | 0x40000000; // SDLK_SCANCODE_MASK
            default: return 0;
        }
    }

    @Inject(method = "acceptKeyCode", at = @At("HEAD"), cancellable = true, remap = false)
    private void janus$fillKeycode(int key, int keycode, int modifiers, CallbackInfoReturnable<Boolean> cir) {
        int sdl = keycode == 0 ? janus$sdlKeycode(key) : 0;
        if (sdl != 0) {
            janus$refocus();
            cir.setReturnValue(this.editBox.keyPressed(new KeyEvent(key, sdl, modifiers)));
        }
    }

    @Inject(method = "moveCursor", at = @At("HEAD"), remap = false)
    private void janus$beforeMove(int amount, CallbackInfoReturnable<Boolean> cir) {
        janus$refocus();
    }
}
