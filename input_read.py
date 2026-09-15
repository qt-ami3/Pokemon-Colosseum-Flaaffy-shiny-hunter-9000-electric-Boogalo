import pygame

pygame.init()
pygame.joystick.init()
controller = pygame.joystick.Joystick(0)
controller.init()

running = True
while running:
    for event in pygame.event.get():
        if event.type == pygame.JOYBUTTONDOWN:
            print(f"Button {event.button} pressed")
        elif event.type == pygame.JOYBUTTONUP:
            print(f"Button {event.button} released")
        elif event.type == pygame.JOYAXISMOTION:
            print(f"Axis {event.axis} value: {event.value}")
